"""Chartable series payloads for upstream UIs.

These builders share the same market-data and indicator math as the rendered
chart endpoints, but return JSON series/levels/tables instead of PNG images so
clients can draw their own charts.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import numpy as np
import pandas as pd

from app.charts import (
    FlowCompassConfig,
    RidgeGrowthConfig,
    calculate_auction_levels,
    calculate_auction_observation,
    calculate_flow_compass_indicator,
    calculate_ridge_growth_strategy,
    normalized_benchmark_series,
    series_annualized_volatility,
    series_max_drawdown,
    series_points,
    series_return,
    terminal_ema,
    terminal_sma,
)
from app.market_data import HistoryResult
from app.torque import (
    COMPONENT_WEIGHTS,
    compute_torque_score,
    _gross_margin_series,
    _operating_margin_series,
    _revenue_series,
    _sec_available,
)

MONTH_NAMES = {
    1: "Jan",
    2: "Feb",
    3: "Mar",
    4: "Apr",
    5: "May",
    6: "Jun",
    7: "Jul",
    8: "Aug",
    9: "Sep",
    10: "Oct",
    11: "Nov",
    12: "Dec",
}


def ohlcv_points(history: HistoryResult) -> list[dict[str, float | str]]:
    rows: list[dict[str, float | str]] = []
    for timestamp, row in history.data.iterrows():
        rows.append(
            {
                "date": timestamp.date().isoformat(),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]) if "Volume" in row and pd.notna(row["Volume"]) else 0.0,
            }
        )
    return rows


def frame_points(
    frame: pd.DataFrame, columns: list[str]
) -> list[dict[str, float | str | bool | None]]:
    points: list[dict[str, float | str | bool | None]] = []
    for timestamp, row in frame.iterrows():
        point: dict[str, float | str | bool | None] = {
            "date": timestamp.date().isoformat()
        }
        for column in columns:
            if column not in frame.columns:
                continue
            value = row[column]
            if isinstance(value, (bool, np.bool_)):
                point[column] = bool(value)
            elif pd.isna(value):
                point[column] = None
            elif isinstance(value, (int, np.integer)):
                point[column] = int(value)
            elif isinstance(value, (float, np.floating)):
                point[column] = float(value)
            else:
                point[column] = str(value)
        points.append(point)
    return points


def build_auction_chart_data(history: HistoryResult, *, period: str) -> dict[str, Any]:
    vah, val, poc = calculate_auction_levels(history.data)
    observation = calculate_auction_observation(history)
    meta = {"vah": vah, "val": val, "poc": poc}
    return {
        "chart_type": "auction",
        "ticker": history.ticker,
        "period": period,
        "provider": history.provider,
        "provider_note": history.note,
        "meta": {**meta, **observation},
        "levels": meta,
        "series": {
            "ohlcv": ohlcv_points(history),
            "close": series_points(history.data["Close"]),
        },
    }


def build_performance_table(
    history: HistoryResult, *, month: int
) -> tuple[pd.DataFrame, dict[str, Any]]:
    data = history.data.copy()
    data["month"] = data.index.month
    data["year"] = data.index.year
    data["pct_change"] = data["Adj Close"].pct_change() * 100
    monthly = (
        data.resample("ME").agg({"pct_change": "sum", "month": "first", "year": "first"}).dropna()
    )
    if monthly.empty:
        raise ValueError("Not enough history for monthly performance")
    current_year = int(monthly["year"].max())
    years = list(range(current_year - 9, current_year + 1))
    table = pd.DataFrame(index=range(1, 13), columns=years, dtype=float)
    for year in years:
        rows = monthly[monthly["year"] == year]
        table.loc[rows["month"].astype(int), year] = rows["pct_change"].to_numpy()

    last_five = years[-5:]
    table["Mean 5Y"] = table[last_five].mean(axis=1)
    table["Median 5Y"] = table[last_five].median(axis=1)
    order = list(range(month, 13)) + list(range(1, month))
    table = table.loc[order]
    selected_mean = (
        float(table.loc[month, "Mean 5Y"]) if pd.notna(table.loc[month, "Mean 5Y"]) else 0.0
    )
    meta = {
        "selected_month": MONTH_NAMES[month],
        "mean_5y": selected_mean,
    }
    return table, meta


def build_performance_chart_data(history: HistoryResult, *, month: int) -> dict[str, Any]:
    table, meta = build_performance_table(history, month=month)
    grid_rows: list[dict[str, Any]] = []
    for month_index, row in table.iterrows():
        cells: dict[str, float | None] = {}
        for column, value in row.items():
            key = str(column)
            cells[key] = float(value) if pd.notna(value) else None
        grid_rows.append(
            {
                "month": int(month_index),
                "month_label": MONTH_NAMES[int(month_index)],
                "values": cells,
            }
        )
    return {
        "chart_type": "performance",
        "ticker": history.ticker,
        "provider": history.provider,
        "provider_note": history.note,
        "meta": meta,
        "table": {
            "columns": [str(column) for column in table.columns],
            "rows": grid_rows,
        },
    }


def build_regression_frame(history: HistoryResult) -> tuple[pd.DataFrame, dict[str, Any]]:
    data = history.data.copy()
    data["ema21"] = data["Close"].ewm(span=21, adjust=False).mean()
    data["ema50"] = data["Close"].ewm(span=50, adjust=False).mean()
    data["ema200"] = data["Close"].ewm(span=200, adjust=False).mean()
    x_values = np.arange(len(data))
    y_values = data["Close"].to_numpy(dtype=float)
    slope, intercept = np.polyfit(x_values, y_values, 1)
    trend = slope * x_values + intercept
    residual_std = float(np.std(y_values - trend))
    data["trend"] = trend
    data["upper_band"] = trend + residual_std
    data["lower_band"] = trend - residual_std
    meta = {
        "slope_per_day": float(slope),
        "residual_std": residual_std,
        "intercept": float(intercept),
    }
    return data, meta


def build_regression_chart_data(history: HistoryResult) -> dict[str, Any]:
    data, meta = build_regression_frame(history)
    return {
        "chart_type": "regression",
        "ticker": history.ticker,
        "provider": history.provider,
        "provider_note": history.note,
        "meta": meta,
        "series": {
            "ohlcv": ohlcv_points(history),
            "close": series_points(data["Close"]),
            "trend": series_points(data["trend"]),
            "upper_band": series_points(data["upper_band"]),
            "lower_band": series_points(data["lower_band"]),
            "ema21": series_points(data["ema21"]),
            "ema50": series_points(data["ema50"]),
            "ema200": series_points(data["ema200"]),
            "volume": series_points(data["Volume"]),
        },
    }


def build_portfolio_chart_data(
    histories: list[HistoryResult],
    *,
    investment_per_stock: float,
    benchmark: HistoryResult | None = None,
) -> dict[str, Any]:
    combined = pd.DataFrame()
    final_values: dict[str, float] = {}
    holdings: dict[str, list[dict[str, float | str]]] = {}
    for history in histories:
        prices = history.data["Adj Close"].dropna()
        normalized = prices / prices.iloc[0] * investment_per_stock
        combined[history.ticker] = normalized
        final_values[history.ticker] = float(normalized.iloc[-1])
        holdings[history.ticker] = series_points(normalized)

    combined["Portfolio"] = combined.sum(axis=1, min_count=1)
    portfolio = combined["Portfolio"].dropna()
    benchmark_series = normalized_benchmark_series(
        benchmark, portfolio, investment_per_stock, histories
    )
    meta: dict[str, Any] = {
        "final_values": final_values,
        "initial_value": float(portfolio.iloc[0]),
        "portfolio_final": float(portfolio.iloc[-1]),
        "total_return": series_return(portfolio),
        "max_drawdown": series_max_drawdown(portfolio),
        "annualized_volatility": series_annualized_volatility(portfolio),
        "equity_curve": series_points(portfolio),
        "investment_per_stock": investment_per_stock,
    }
    series: dict[str, Any] = {
        "portfolio": series_points(portfolio),
        "holdings": holdings,
    }
    if benchmark is not None and benchmark_series is not None:
        comparison = pd.concat(
            [portfolio.rename("Portfolio"), benchmark_series.rename("Benchmark")],
            axis=1,
        ).dropna()
        if not comparison.empty:
            benchmark_return = series_return(comparison["Benchmark"])
            portfolio_shared_return = series_return(comparison["Portfolio"])
            meta.update(
                {
                    "benchmark_ticker": benchmark.ticker,
                    "benchmark_return": benchmark_return,
                    "alpha_vs_benchmark": portfolio_shared_return - benchmark_return,
                    "benchmark_final": float(comparison["Benchmark"].iloc[-1]),
                    "benchmark_equity_curve": series_points(benchmark_series),
                }
            )
            series["benchmark"] = series_points(benchmark_series)

    return {
        "chart_type": "portfolio",
        "tickers": [history.ticker for history in histories],
        "provider": "+".join(sorted({history.provider for history in histories})),
        "meta": meta,
        "series": series,
    }


def build_volatility_chart_data(histories: list[HistoryResult]) -> dict[str, Any]:
    rows: list[dict[str, float | str]] = []
    for history in histories:
        close = history.data["Adj Close"].dropna()
        returns = close.pct_change().dropna()
        daily_vol = float(returns.std()) if not returns.empty else 0.0
        annual_vol = daily_vol * float(np.sqrt(252))
        price = float(close.iloc[-1])
        rows.append(
            {
                "ticker": history.ticker,
                "price": price,
                "daily_vol": daily_vol,
                "annual_vol": annual_vol,
                "one_week_range": price * annual_vol * np.sqrt(5 / 252),
                "one_month_range": price * annual_vol * np.sqrt(21 / 252),
            }
        )
    rows = sorted(rows, key=lambda row: float(row["annual_vol"]), reverse=True)
    return {
        "chart_type": "volatility",
        "tickers": [str(row["ticker"]) for row in rows],
        "provider": "+".join(sorted({history.provider for history in histories})),
        "meta": {"rows": rows},
        "rows": rows,
    }


def build_ridge_growth_chart_data(
    history: HistoryResult,
    *,
    period: str,
    config: RidgeGrowthConfig | None = None,
) -> dict[str, Any]:
    signal_frame, meta = calculate_ridge_growth_strategy(history, config)
    _flow_frame, flow_meta = calculate_flow_compass_indicator(history)
    auction_meta = calculate_auction_observation(history)
    meta = {
        **meta,
        "period": period,
        "flow_compass": flow_meta,
        "auction": auction_meta,
    }
    return {
        "chart_type": "ridge-growth",
        "ticker": history.ticker,
        "period": period,
        "provider": history.provider,
        "provider_note": history.note,
        "meta": meta,
        "series": {
            "ohlcv": ohlcv_points(history),
            "close": series_points(signal_frame["Close"]),
            "fast_ma": series_points(signal_frame["fast_ma"]),
            "base_ma": series_points(signal_frame["base_ma"]),
            "major_ma": series_points(signal_frame["major_ma"]),
            "equity": series_points(signal_frame["equity"]),
            "signals": frame_points(
                signal_frame,
                [
                    "Close",
                    "Low",
                    "High",
                    "in_trade",
                    "buy_signal",
                    "sell_signal",
                    "trend_on",
                    "trend_confirmed",
                    "rsi_14",
                ],
            ),
        },
    }


def build_flow_compass_chart_data(
    history: HistoryResult,
    *,
    period: str,
    config: FlowCompassConfig | None = None,
) -> dict[str, Any]:
    signal_frame, meta = calculate_flow_compass_indicator(history, config)
    cfg = config or FlowCompassConfig()
    meta = {**meta, "period": period}
    return {
        "chart_type": "flow-compass",
        "ticker": history.ticker,
        "period": period,
        "provider": history.provider,
        "provider_note": history.note,
        "meta": meta,
        "levels": {
            "trigger_level": cfg.trigger_level,
            "strong_level": cfg.strong_level,
        },
        "series": {
            "ohlcv": ohlcv_points(history),
            "close": series_points(signal_frame["Close"]),
            "flow_score": series_points(signal_frame["flow_score"]),
            "compass_signal": series_points(signal_frame["compass_signal"]),
            "signals": frame_points(
                signal_frame,
                [
                    "Close",
                    "Low",
                    "High",
                    "flow_score",
                    "compass_signal",
                    "volume_score",
                    "trend_score",
                    "momentum_score",
                    "value_score",
                    "rvi_score",
                    "fresh_long",
                    "fresh_short",
                    "long_ok",
                    "short_ok",
                    "state",
                ],
            ),
        },
    }


def build_torque_chart_data(
    *,
    history: HistoryResult | None,
    sec_trend: dict[str, Any] | None,
    profile: dict[str, Any] | None,
) -> dict[str, Any]:
    torque = compute_torque_score(
        history=history, sec_trend=sec_trend, profile=profile
    )
    ticker = (
        history.ticker
        if isinstance(history, HistoryResult)
        else str(profile.get("symbol") if isinstance(profile, dict) else "TICKER")
    )
    rev_labels, rev_values = _revenue_series(sec_trend)
    gm_values = _gross_margin_series(sec_trend)
    om_values = _operating_margin_series(sec_trend)

    price_series: dict[str, Any] = {}
    if isinstance(history, HistoryResult) and not history.data.empty and "Close" in history.data:
        close = pd.to_numeric(history.data["Close"], errors="coerce").dropna()
        if not close.empty:
            price_series = {
                "close": series_points(close),
                "ema75": series_points(terminal_ema(close, 75)),
                "sma200": series_points(terminal_sma(close, 200)),
                "sma50": series_points(terminal_sma(close, 50)),
                "ohlcv": ohlcv_points(history),
            }

    fundamentals = {
        "revenue": [
            {"label": label, "value": value}
            for label, value in zip(rev_labels, rev_values, strict=False)
        ],
        "gross_margin": [
            {"label": rev_labels[-len(gm_values) + index] if rev_labels else str(index), "value": value}
            for index, value in enumerate(gm_values)
        ]
        if gm_values
        else [],
        "operating_margin": [
            {"label": rev_labels[-len(om_values) + index] if rev_labels else str(index), "value": value}
            for index, value in enumerate(om_values)
        ]
        if om_values
        else [],
    }

    meta = {
        "ticker": ticker,
        "total_score": torque.total_score,
        "stage_label": torque.stage_label,
        "stage_detail": torque.stage_detail,
        "recommendation": torque.recommendation,
        "target_zone": torque.target_zone,
        "components": {
            component.name: {
                "score": round(component.score, 2),
                "weight": component.weight,
                "detail": component.detail,
            }
            for component in torque.components
        },
        "weights": dict(COMPONENT_WEIGHTS),
        "fundamental_data_available": _sec_available(sec_trend),
    }
    return {
        "chart_type": "torque",
        "ticker": ticker,
        "provider": history.provider if isinstance(history, HistoryResult) else "n/a",
        "provider_note": history.note if isinstance(history, HistoryResult) else "",
        "meta": meta,
        "torque": asdict(torque),
        "series": {
            "price": price_series,
            "fundamentals": fundamentals,
        },
    }
