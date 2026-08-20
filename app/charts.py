from __future__ import annotations

import base64
import math
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.patheffects as path_effects
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.ticker import FuncFormatter

from app.market_data import HistoryResult, series_timestamp_label

CHART_BG = "#05070a"
AX_BG = "#081016"
PANEL = "#0d171d"
GRID = "#24444a"
TEXT = "#fff4c2"
TEXT_STRONG = "#fff9d9"
MUTED = "#9fb0a8"
AMBER = "#ffc94a"
AMBER_HOT = "#ffe66f"
GREEN = "#79ff9c"
CYAN = "#57d9ff"
RED = "#ff695d"
VIOLET = "#b28cff"
ORANGE = "#ffae57"

RETURN_CMAP = LinearSegmentedColormap.from_list(
    "underlying_terminal_returns",
    [
        (0.0, "#ff4d5a"),
        (0.42, "#172126"),
        (0.5, "#263237"),
        (0.72, GREEN),
        (1.0, CYAN),
    ],
)


@dataclass(frozen=True)
class RenderedImage:
    data: str
    mime: str
    filename: str


@dataclass(frozen=True)
class RidgeGrowthConfig:
    initial_capital: float = 10_000.0
    growth_pct: float = 90.0
    use_cash_cap: bool = True
    max_cash: float = 30_000.0
    fast_len: int = 75
    base_len: int = 150
    major_len: int = 200
    entry_confirm_bars: int = 2
    exit_style: str = "Major Breakdown"
    commission_percent: float = 0.01


@dataclass(frozen=True)
class FlowCompassConfig:
    norm_len: int = 100
    signal_len: int = 9
    trigger_level: float = 25.0
    strong_level: float = 55.0


@contextmanager
def managed_figure(fig: plt.Figure) -> Iterator[plt.Figure]:
    """Guarantee a figure is released even if rendering raises before it is saved.

    On the normal path the caller hands ``fig`` to ``image_from_figure``, which closes
    it; ``plt.close`` is idempotent so the redundant close here is a harmless no-op. On
    an exception mid-render the figure would otherwise leak and grow worker memory, so we
    close it and re-raise the original error unchanged.
    """
    try:
        yield fig
    except BaseException:
        plt.close(fig)
        raise


def image_from_figure(fig: plt.Figure, filename: str) -> RenderedImage:
    buffer = BytesIO()
    fig.savefig(
        buffer,
        format="png",
        bbox_inches="tight",
        facecolor=fig.get_facecolor(),
        pad_inches=0.18,
        dpi=170,
    )
    plt.close(fig)
    buffer.seek(0)
    return RenderedImage(
        data=base64.b64encode(buffer.getvalue()).decode("ascii"),
        mime="image/png",
        filename=filename,
    )


# Built once at import time; ``apply_terminal_style`` reapplies these to the global
# rcParams on each render (rcParams is shared mutable state) without rebuilding the dict.
_TERMINAL_RCPARAMS: dict[str, Any] = {
    "figure.facecolor": CHART_BG,
    "savefig.facecolor": CHART_BG,
    "axes.facecolor": AX_BG,
    "axes.edgecolor": AMBER,
    "axes.labelcolor": MUTED,
    "axes.titlecolor": AMBER,
    "axes.titleweight": "bold",
    "axes.titlesize": 15,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "grid.color": GRID,
    "grid.linestyle": "-",
    "grid.linewidth": 0.55,
    "legend.facecolor": PANEL,
    "legend.edgecolor": AMBER,
    "legend.labelcolor": TEXT,
    "font.family": "DejaVu Sans",
    "text.color": TEXT,
    "figure.dpi": 150,
}


def apply_terminal_style() -> None:
    plt.rcParams.update(_TERMINAL_RCPARAMS)  # type: ignore[arg-type]


def style_axis(
    ax: Any,
    *,
    title: str | None = None,
    subtitle: str | None = None,
    grid_axis: str = "both",
) -> None:
    ax.set_facecolor(AX_BG)
    for spine in ax.spines.values():
        spine.set_color(AMBER)
        spine.set_alpha(0.38)
        spine.set_linewidth(1.0)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.grid(True, axis=grid_axis, alpha=0.42)
    if title:
        ax.set_title(title.upper(), loc="left", pad=18, color=AMBER_HOT, fontsize=15)
    if subtitle:
        ax.text(
            0,
            1.015,
            subtitle,
            transform=ax.transAxes,
            ha="left",
            va="bottom",
            color=MUTED,
            fontsize=9,
            fontweight="bold",
        )


def style_legend(ax: Any, *, loc: str = "upper left", ncols: int = 1) -> None:
    legend = ax.legend(loc=loc, ncols=ncols, frameon=True, fontsize=8)
    if legend is None:
        return
    frame = legend.get_frame()
    frame.set_facecolor(PANEL)
    frame.set_edgecolor(AMBER)
    frame.set_alpha(0.86)
    for text in legend.get_texts():
        text.set_color(TEXT)


def add_terminal_footer(fig: plt.Figure, *, left: str, right: str | None = None) -> None:
    fig.text(
        0.012,
        0.018,
        left.upper(),
        ha="left",
        color=GREEN,
        fontsize=8,
        fontweight="bold",
        family="DejaVu Sans Mono",
    )
    fig.text(
        0.988,
        0.018,
        (right or "UNDERLYING TERMINAL").upper(),
        ha="right",
        color=AMBER,
        fontsize=8,
        fontweight="bold",
        family="DejaVu Sans Mono",
    )


def label_last_value(
    ax: Any,
    *,
    x_value: Any,
    y_value: float,
    label: str,
    color: str,
    offset: tuple[int, int] = (9, 0),
) -> None:
    ax.annotate(
        label,
        xy=(x_value, y_value),
        xytext=offset,
        textcoords="offset points",
        ha="left",
        va="center",
        color=CHART_BG,
        fontsize=8,
        fontweight="bold",
        bbox={
            "boxstyle": "round,pad=0.28,rounding_size=0.1",
            "facecolor": color,
            "edgecolor": color,
            "alpha": 0.92,
        },
    )


def glow_effect(width: float = 3.2, color: str = CHART_BG) -> list[Any]:
    return [path_effects.Stroke(linewidth=width, foreground=color), path_effects.Normal()]


def format_currency_y_axis(ax: Any) -> None:
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _position: f"\\${value:,.0f}"))


def format_absolute_y_axis(ax: Any) -> None:
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _position: f"{abs(value):,.0f}"))


def calculate_auction_levels(data: pd.DataFrame) -> tuple[float, float, float]:
    window = data.tail(22).head(21)
    if window.empty:
        raise ValueError("At least 21 rows are required for auction levels")
    vah = float(window["High"].max())
    val = float(window["Low"].min())
    poc = float(window["Close"].median())
    return vah, val, poc


def terminal_ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def terminal_sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length, min_periods=length).mean()


def terminal_stdev(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length, min_periods=1).std(ddof=0)


def terminal_rsi(series: pd.Series, length: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(avg_loss != 0, 100)
    rsi = rsi.where(avg_gain != 0, 0)
    rsi = rsi.where(~((avg_gain == 0) & (avg_loss == 0)), 50)
    return rsi.fillna(50)


def terminal_vwma(price: pd.Series, volume: pd.Series, length: int) -> pd.Series:
    volume_sum = volume.rolling(length, min_periods=1).sum()
    weighted_sum = (price * volume).rolling(length, min_periods=1).sum()
    return weighted_sum / volume_sum.replace(0, np.nan)


def normalized_score(series: pd.Series, length: int) -> pd.Series:
    low = series.rolling(length, min_periods=1).min()
    high = series.rolling(length, min_periods=1).max()
    span = high - low
    score = (series - low) / span.replace(0, np.nan) * 100
    return score.fillna(50)


def clamp_series(series: pd.Series, low: float, high: float) -> pd.Series:
    return series.clip(lower=low, upper=high)


def center_score(series: pd.Series) -> pd.Series:
    return clamp_series((series - 50.0) * 2.0, -100.0, 100.0)


def impulse_score(series: pd.Series, length: int) -> pd.Series:
    base = normalized_score(series, length)
    impulse = base + (base - base.shift(3).fillna(base))
    return center_score(impulse).fillna(0)


def bars_since_trend_off(trend_on: pd.Series) -> pd.Series:
    values: list[float] = []
    last_off_index: int | None = None
    for index, value in enumerate(trend_on.fillna(False).to_list()):
        if not bool(value):
            last_off_index = index
            values.append(0.0)
        elif last_off_index is None:
            values.append(np.nan)
        else:
            values.append(float(index - last_off_index))
    return pd.Series(values, index=trend_on.index)


def safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not bool(np.isfinite(number)):
        return None
    return number


def latest_float(series: pd.Series) -> float | None:
    if series.empty:
        return None
    return safe_float(series.iloc[-1])


def pct_or_none(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator - 1


def calculate_ridge_growth_strategy(
    history: HistoryResult, config: RidgeGrowthConfig | None = None
) -> tuple[pd.DataFrame, dict[str, Any]]:
    cfg = config or RidgeGrowthConfig()
    data = history.data.copy().dropna(subset=["Close"])
    if data.empty:
        raise ValueError("At least one close is required for Ridge Growth")

    close = pd.to_numeric(data["Close"], errors="coerce").ffill()
    fast_ma = terminal_ema(close, cfg.fast_len)
    base_ma = terminal_ema(close, cfg.base_len)
    major_ma = terminal_sma(close, cfg.major_len)
    trend_on = fast_ma > base_ma
    since_off = bars_since_trend_off(trend_on)
    trend_confirmed = trend_on & (
        since_off.fillna(float(cfg.entry_confirm_bars)) >= cfg.entry_confirm_bars - 1
    )
    rsi_14 = terminal_rsi(close, 14)
    fast_base_cross_exit = (fast_ma < base_ma) & (fast_ma.shift(1) >= base_ma.shift(1))
    major_break_exit = (close < major_ma) & (fast_ma < base_ma)
    emergency_exit = (close < major_ma) & (close < base_ma) & (rsi_14 < 40.0)

    cash = cfg.initial_capital
    shares = 0
    open_trade: dict[str, Any] | None = None
    commission_rate = cfg.commission_percent / 100.0
    trades: list[dict[str, Any]] = []
    raw_cash_values: list[float] = []
    cash_to_use_values: list[float] = []
    shares_to_buy_values: list[int] = []
    equity_values: list[float] = []
    in_trade_values: list[bool] = []
    buy_values: list[bool] = []
    sell_values: list[bool] = []

    for timestamp, close_value in close.items():
        price = float(close_value)
        equity_before = cash + shares * price
        raw_cash = equity_before * cfg.growth_pct / 100.0
        cash_to_use = min(raw_cash, cfg.max_cash) if cfg.use_cash_cap else raw_cash
        shares_to_buy = int(math.floor(cash_to_use / price)) if price > 0 else 0
        in_trade = shares > 0

        exit_signal = False
        if in_trade:
            if cfg.exit_style == "Fast/Base Cross":
                exit_signal = bool(fast_base_cross_exit.loc[timestamp])
            elif cfg.exit_style == "Emergency Only":
                exit_signal = bool(emergency_exit.loc[timestamp])
            else:
                exit_signal = bool(major_break_exit.loc[timestamp])

        buy_signal = (not in_trade) and bool(trend_confirmed.loc[timestamp]) and shares_to_buy >= 1
        if buy_signal:
            affordable_qty = int(math.floor(cash / (price * (1 + commission_rate))))
            quantity = min(shares_to_buy, affordable_qty)
            if quantity >= 1:
                fee = quantity * price * commission_rate
                cash -= quantity * price + fee
                shares = quantity
                open_trade = {
                    "entry_date": timestamp.date().isoformat(),
                    "entry_price": price,
                    "quantity": quantity,
                    "entry_fee": fee,
                }
            else:
                buy_signal = False
        elif exit_signal and shares > 0:
            fee = shares * price * commission_rate
            cash += shares * price - fee
            if open_trade is not None:
                trade_entry_price = float(open_trade["entry_price"])
                quantity = int(open_trade["quantity"])
                entry_fee = float(open_trade["entry_fee"])
                entry_value = trade_entry_price * quantity + entry_fee
                exit_value = price * quantity - fee
                trades.append(
                    {
                        "entry_date": open_trade["entry_date"],
                        "exit_date": timestamp.date().isoformat(),
                        "quantity": quantity,
                        "entry_price": trade_entry_price,
                        "exit_price": price,
                        "pnl": exit_value - entry_value,
                        "return": exit_value / entry_value - 1 if entry_value else 0.0,
                    }
                )
            shares = 0
            open_trade = None

        equity_after = cash + shares * price
        raw_cash_values.append(raw_cash)
        cash_to_use_values.append(cash_to_use)
        shares_to_buy_values.append(shares_to_buy)
        equity_values.append(equity_after)
        in_trade_values.append(shares > 0)
        buy_values.append(buy_signal)
        sell_values.append(exit_signal and not buy_signal)

    signal_frame = data.copy()
    signal_frame["fast_ma"] = fast_ma
    signal_frame["base_ma"] = base_ma
    signal_frame["major_ma"] = major_ma
    signal_frame["trend_on"] = trend_on
    signal_frame["trend_confirmed"] = trend_confirmed
    signal_frame["rsi_14"] = rsi_14
    signal_frame["raw_cash"] = raw_cash_values
    signal_frame["cash_to_use"] = cash_to_use_values
    signal_frame["shares_to_buy"] = shares_to_buy_values
    signal_frame["equity"] = equity_values
    signal_frame["in_trade"] = in_trade_values
    signal_frame["buy_signal"] = buy_values
    signal_frame["sell_signal"] = sell_values

    equity_series = signal_frame["equity"]
    ending_equity = float(equity_series.iloc[-1])
    max_drawdown = series_max_drawdown(equity_series)
    latest = signal_frame.iloc[-1]
    latest_close = safe_float(latest["Close"])
    entry_price = safe_float(open_trade["entry_price"]) if open_trade else None
    state = "LONG" if bool(latest["in_trade"]) else "WATCH" if bool(latest["trend_on"]) else "CASH"
    if bool(latest["buy_signal"]):
        recommendation = "BUY"
    elif bool(latest["sell_signal"]):
        recommendation = "SELL"
    elif bool(latest["in_trade"]):
        recommendation = "HOLD LONG"
    elif bool(latest["trend_confirmed"]):
        recommendation = "BUY SETUP"
    elif bool(latest["trend_on"]):
        recommendation = "WATCH"
    else:
        recommendation = "CASH"

    wins = [trade for trade in trades if float(trade["pnl"]) > 0]
    meta: dict[str, Any] = {
        "ticker": history.ticker,
        "state": state,
        "recommendation": recommendation,
        "ending_equity": ending_equity,
        "total_return": ending_equity / cfg.initial_capital - 1,
        "max_drawdown": max_drawdown,
        "closed_trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "buy_count": int(signal_frame["buy_signal"].sum()),
        "sell_count": int(signal_frame["sell_signal"].sum()),
        "open_position_qty": int(shares),
        "open_position_return": pct_or_none(latest_close, entry_price),
        "latest_close": latest_close,
        "fast_ema": latest_float(signal_frame["fast_ma"]),
        "base_ema": latest_float(signal_frame["base_ma"]),
        "major_sma": latest_float(signal_frame["major_ma"]),
        "trend_confirmed": bool(latest["trend_confirmed"]),
        "cash_to_use": safe_float(latest["cash_to_use"]),
        "shares_to_buy": int(latest["shares_to_buy"]),
        "exit_style": cfg.exit_style,
        "large_cap_caveat": (
            "Designed for persistent large-cap trend behavior; thin or mean-reverting "
            "symbols need stricter review."
        ),
        "trades": trades[-10:],
        "equity_curve": series_points(equity_series),
    }
    return signal_frame, meta


def calculate_flow_compass_indicator(
    history: HistoryResult, config: FlowCompassConfig | None = None
) -> tuple[pd.DataFrame, dict[str, Any]]:
    cfg = config or FlowCompassConfig()
    data = history.data.copy().dropna(subset=["Close"])
    if data.empty:
        raise ValueError("At least one close is required for Flow Compass")

    close = pd.to_numeric(data["Close"], errors="coerce").ffill()
    open_price = pd.to_numeric(data.get("Open", close), errors="coerce").fillna(close)
    high = pd.to_numeric(data.get("High", close), errors="coerce").fillna(close)
    low = pd.to_numeric(data.get("Low", close), errors="coerce").fillna(close)
    volume = pd.to_numeric(data.get("Volume", pd.Series(0, index=data.index)), errors="coerce")
    volume = volume.fillna(0)

    close_delta = close.diff()
    signed_volume = pd.Series(
        np.select(
            [
                close > open_price,
                close < open_price,
                close > close.shift(1),
                close < close.shift(1),
            ],
            [volume, -volume, volume, -volume],
            default=0.0,
        ),
        index=data.index,
    )
    cvd = signed_volume.cumsum()
    volume_score = impulse_score(cvd, cfg.norm_len)

    rsi_14 = terminal_rsi(close, 14)
    rsi_3 = terminal_rsi(close, 3)
    momentum_raw = (rsi_14 - rsi_14.shift(1)).fillna(0) + terminal_sma(rsi_3, 3).fillna(50)
    momentum_score = impulse_score(momentum_raw, cfg.norm_len)

    ema_13 = terminal_ema(close, 13)
    sma_30 = terminal_sma(close, 30)
    trend_raw = ((ema_13 - sma_30) / sma_30.replace(0, np.nan)).fillna(0)
    trend_score = impulse_score(trend_raw, cfg.norm_len)

    hlc3 = (high + low + close) / 3.0
    value_mid = terminal_vwma(hlc3, volume, cfg.norm_len)
    value_dev = terminal_stdev(hlc3, cfg.norm_len)
    value_score = clamp_series(
        ((close - value_mid) / (2.0 * value_dev.replace(0, np.nan)) * 100.0).fillna(0),
        -100.0,
        100.0,
    )

    rvi_len = 10
    close_stdev = terminal_stdev(close, rvi_len)
    upper_dev = close_stdev.where(close_delta > 0, 0.0)
    lower_dev = close_stdev.where(close_delta <= 0, 0.0)
    upper = terminal_ema(upper_dev, rvi_len)
    lower = terminal_ema(lower_dev, rvi_len)
    rvi = (upper / (upper + lower).replace(0, np.nan) * 100.0).fillna(50.0)
    rvi_score = center_score(rvi)

    raw_score = (
        volume_score * 0.28
        + trend_score * 0.25
        + momentum_score * 0.20
        + value_score * 0.17
        + rvi_score * 0.10
    )
    flow_score = terminal_ema(raw_score.fillna(0), cfg.signal_len)
    compass_signal = terminal_ema(flow_score, cfg.signal_len)
    long_ok = (flow_score > cfg.trigger_level) & (flow_score > compass_signal)
    short_ok = (flow_score < -cfg.trigger_level) & (flow_score < compass_signal)
    strong_long = (flow_score > cfg.strong_level) & (flow_score > compass_signal)
    strong_short = (flow_score < -cfg.strong_level) & (flow_score < compass_signal)
    fresh_long = long_ok & ~long_ok.shift(1, fill_value=False)
    fresh_short = short_ok & ~short_ok.shift(1, fill_value=False)

    states: list[str] = []
    for is_strong_long, is_long, is_strong_short, is_short in zip(
        strong_long, long_ok, strong_short, short_ok, strict=False
    ):
        if bool(is_strong_long):
            states.append("STRONG LONG")
        elif bool(is_long):
            states.append("LONG OK")
        elif bool(is_strong_short):
            states.append("STRONG SHORT")
        elif bool(is_short):
            states.append("AVOID CALLS")
        else:
            states.append("NEUTRAL")

    signal_frame = data.copy()
    signal_frame["cvd"] = cvd
    signal_frame["volume_score"] = volume_score
    signal_frame["momentum_score"] = momentum_score
    signal_frame["trend_score"] = trend_score
    signal_frame["value_score"] = value_score
    signal_frame["rvi_score"] = rvi_score
    signal_frame["flow_score"] = flow_score
    signal_frame["compass_signal"] = compass_signal
    signal_frame["long_ok"] = long_ok
    signal_frame["short_ok"] = short_ok
    signal_frame["strong_long"] = strong_long
    signal_frame["strong_short"] = strong_short
    signal_frame["fresh_long"] = fresh_long
    signal_frame["fresh_short"] = fresh_short
    signal_frame["state"] = states

    latest = signal_frame.iloc[-1]
    meta = {
        "ticker": history.ticker,
        "state": str(latest["state"]),
        "score": safe_float(latest["flow_score"]),
        "signal": safe_float(latest["compass_signal"]),
        "volume_score": safe_float(latest["volume_score"]),
        "trend_score": safe_float(latest["trend_score"]),
        "momentum_score": safe_float(latest["momentum_score"]),
        "value_score": safe_float(latest["value_score"]),
        "rvi_score": safe_float(latest["rvi_score"]),
        "fresh_long": bool(latest["fresh_long"]),
        "fresh_short": bool(latest["fresh_short"]),
        "trigger_level": cfg.trigger_level,
        "strong_level": cfg.strong_level,
        "delta_method": "daily signed-volume proxy",
    }
    return signal_frame, meta


def calculate_auction_observation(history: HistoryResult) -> dict[str, Any]:
    vah, val, poc = calculate_auction_levels(history.data)
    latest_close = float(history.data["Close"].dropna().iloc[-1])
    if latest_close > vah:
        location = "above value"
    elif latest_close < val:
        location = "below value"
    else:
        location = "inside value"
    return {
        "vah": vah,
        "val": val,
        "poc": poc,
        "location": location,
        "distance_to_poc": latest_close / poc - 1 if poc else 0.0,
    }


def build_ridge_growth_memo(ticker: str, windows: list[dict[str, Any]]) -> str:
    primary = next((window for window in windows if window.get("period") == "1y"), windows[-1])
    flow = primary.get("flow_compass", {})
    auction = primary.get("auction", {})
    lines = [
        f"### {ticker} Ridge + Flow Read",
        "",
        (
            f"Ridge is {primary.get('state')} with a {primary.get('recommendation')} read on "
            f"the 1D {primary.get('period')} window. Ending strategy equity is "
            f"${float(primary.get('ending_equity') or 0):,.0f}, with "
            f"{float(primary.get('total_return') or 0) * 100:.1f}% total return and "
            f"{float(primary.get('max_drawdown') or 0) * 100:.1f}% max drawdown."
        ),
        (
            f"Flow Compass confirms the tape as {flow.get('state', 'UNKNOWN')} with score "
            f"{float(flow.get('score') or 0):.1f} versus signal "
            f"{float(flow.get('signal') or 0):.1f}. Volume, trend, momentum, value, and "
            "relative-volatility components are blended into that read."
        ),
        (
            f"Auction context puts price {auction.get('location', 'near value')} around POC "
            f"{float(auction.get('poc') or 0):.2f}, VAH {float(auction.get('vah') or 0):.2f}, "
            f"and VAL {float(auction.get('val') or 0):.2f}. Treat acceptance above VAH as "
            "trend validation and rejection below VAL as a reason to reduce conviction."
        ),
        (
            "This model is intentionally best suited to persistent large-cap trend behavior; "
            "thin, jumpy, or mean-reverting names deserve a separate catalyst and liquidity check."
        ),
    ]
    return "\n\n".join(lines)


def component_color(score: float | None) -> str:
    if score is None:
        return MUTED
    if score > 15:
        return GREEN
    if score < -15:
        return RED
    return MUTED


def render_auction_chart(
    history: HistoryResult, *, period: str
) -> tuple[RenderedImage, dict[str, Any]]:
    apply_terminal_style()
    data = history.data
    vah, val, poc = calculate_auction_levels(data)
    fig, ax = plt.subplots(figsize=(15, 9))
    with managed_figure(fig):
        subtitle = f"{period.upper()} | provider {history.provider}"
        style_axis(ax, title=f"{history.ticker} auction map", subtitle=subtitle)
        ax.plot(
            data.index,
            data["Close"],
            color=TEXT_STRONG,
            linewidth=1.4,
            label=f"{history.ticker} close",
            path_effects=glow_effect(4.0),
        )

        candle_width = max(0.25, min(0.8, 12 / max(len(data), 1)))
        for timestamp, row in data.iterrows():
            color = GREEN if row["Close"] >= row["Open"] else RED
            x_value = mdates.date2num(timestamp)
            ax.vlines(x_value, row["Low"], row["High"], color=color, linewidth=0.9, alpha=0.8)
            ax.vlines(
                x_value,
                row["Open"],
                row["Close"],
                color=color,
                linewidth=candle_width * 4,
                alpha=0.95,
            )

        ax.axhline(vah, color=GREEN, linestyle="--", linewidth=1.9, label=f"VAH {vah:.2f}")
        ax.axhline(val, color=RED, linestyle="--", linewidth=1.9, label=f"VAL {val:.2f}")
        ax.axhline(poc, color=AMBER_HOT, linestyle="-.", linewidth=2.4, label=f"POC {poc:.2f}")
        ax.axhspan(val, vah, color=AMBER, alpha=0.055)
        ax.fill_between(data.index, poc, vah, color=GREEN, alpha=0.11)
        ax.fill_between(data.index, val, poc, color=RED, alpha=0.11)
        label_last_value(
            ax, x_value=data.index[-1], y_value=vah, label=f"VAH {vah:.2f}", color=GREEN
        )
        label_last_value(
            ax, x_value=data.index[-1], y_value=poc, label=f"POC {poc:.2f}", color=AMBER
        )
        label_last_value(ax, x_value=data.index[-1], y_value=val, label=f"VAL {val:.2f}", color=RED)
        ax.set_xlabel("Date")
        ax.set_ylabel("Price")
        style_legend(ax)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        fig.autofmt_xdate()
        fig.tight_layout(rect=(0, 0.05, 1, 0.96))
        add_terminal_footer(
            fig,
            left=f"Fair price {poc:.2f} | high {vah:.2f} | low {val:.2f}",
            right=f"{history.ticker} {period}",
        )
        return image_from_figure(fig, f"{history.ticker.lower()}-auction.png"), {
            "vah": vah,
            "val": val,
            "poc": poc,
        }


def render_performance_chart(
    history: HistoryResult, *, month: int
) -> tuple[RenderedImage, dict[str, Any]]:
    apply_terminal_style()
    data = history.data.copy()
    data["month"] = data.index.month
    data["year"] = data.index.year
    data["pct_change"] = data["Adj Close"].pct_change() * 100
    monthly = (
        data.resample("ME").agg({"pct_change": "sum", "month": "first", "year": "first"}).dropna()
    )
    current_year = int(monthly["year"].max())
    years = list(range(current_year - 9, current_year + 1))
    table = pd.DataFrame(index=range(1, 13), columns=years, dtype=float)
    for year in years:
        rows = monthly[monthly["year"] == year]
        table.loc[rows["month"].astype(int), year] = rows["pct_change"].to_numpy()

    last_five = years[-5:]
    table["Mean 5Y"] = table[last_five].mean(axis=1)
    table["Median 5Y"] = table[last_five].median(axis=1)
    month_names = {
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
    order = list(range(month, 13)) + list(range(1, month))
    table = table.loc[order]
    labels = [month_names[index] for index in table.index]

    fig, ax = plt.subplots(figsize=(15, 8))
    with managed_figure(fig):
        values = table.fillna(0).to_numpy(dtype=float)
        vmax = max(5.0, float(np.nanmax(np.abs(values))))
        image = ax.imshow(values, cmap=RETURN_CMAP, aspect="auto", vmin=-vmax, vmax=vmax)
        style_axis(
            ax,
            title=f"{history.ticker} monthly return grid",
            subtitle=f"Rolling 10 years | rotated from {month_names[month]}",
        )
        ax.set_yticks(np.arange(len(labels)), labels=labels)
        ax.set_xticks(
            np.arange(len(table.columns)),
            labels=[str(column) for column in table.columns],
            rotation=45,
            ha="right",
        )
        ax.set_xticks(np.arange(-0.5, len(table.columns), 1), minor=True)
        ax.set_yticks(np.arange(-0.5, len(labels), 1), minor=True)
        ax.grid(which="minor", color=CHART_BG, linewidth=1.0, alpha=0.85)
        ax.grid(False, which="major")
        for row_index in range(values.shape[0]):
            for column_index in range(values.shape[1]):
                value = table.iloc[row_index, column_index]
                if pd.notna(value):
                    text_color = CHART_BG if value > vmax * 0.34 else TEXT
                    ax.text(
                        column_index,
                        row_index,
                        f"{value:.1f}",
                        ha="center",
                        va="center",
                        color=text_color,
                        fontsize=8,
                        fontweight="bold",
                        path_effects=glow_effect(2.2) if text_color == TEXT else None,
                    )
        colorbar = fig.colorbar(image, ax=ax, label="Monthly return (%)")
        colorbar.ax.yaxis.label.set_color(MUTED)
        colorbar.ax.tick_params(colors=MUTED)
        colorbar_outline: Any = colorbar.outline
        colorbar_outline.set_edgecolor(AMBER)
        colorbar_outline.set_alpha(0.42)
        fig.tight_layout(rect=(0, 0.05, 1, 0.96))
        add_terminal_footer(
            fig,
            left=f"{history.ticker} seasonality map",
            right=f"source {history.provider}",
        )
        selected_mean = (
            float(table.loc[month, "Mean 5Y"]) if pd.notna(table.loc[month, "Mean 5Y"]) else 0.0
        )
        return image_from_figure(fig, f"{history.ticker.lower()}-performance.png"), {
            "selected_month": month_names[month],
            "mean_5y": selected_mean,
        }


def render_regression_chart(history: HistoryResult) -> tuple[RenderedImage, dict[str, Any]]:
    apply_terminal_style()
    data = history.data.copy()
    data["ema21"] = data["Close"].ewm(span=21, adjust=False).mean()
    data["ema50"] = data["Close"].ewm(span=50, adjust=False).mean()
    data["ema200"] = data["Close"].ewm(span=200, adjust=False).mean()
    x_values = np.arange(len(data))
    y_values = data["Close"].to_numpy(dtype=float)
    slope, intercept = np.polyfit(x_values, y_values, 1)
    trend = slope * x_values + intercept
    residual_std = float(np.std(y_values - trend))

    fig, (price_ax, volume_ax) = plt.subplots(
        2, 1, figsize=(15, 10), gridspec_kw={"height_ratios": [3, 1]}
    )
    with managed_figure(fig):
        style_axis(
            price_ax,
            title=f"{history.ticker} regression channel",
            subtitle=f"slope {slope:.4f} per session | residual sigma {residual_std:.2f}",
        )
        price_ax.fill_between(
            data.index,
            trend - residual_std,
            trend + residual_std,
            color=CYAN,
            alpha=0.11,
            label="1 sigma channel",
        )
        price_ax.plot(
            data.index,
            data["Close"],
            color=TEXT_STRONG,
            linewidth=1.35,
            label="Close",
            path_effects=glow_effect(4.0),
        )
        price_ax.plot(data.index, trend, color=AMBER_HOT, linewidth=2.2, label="Regression")
        price_ax.plot(
            data.index,
            trend + residual_std,
            color=GREEN,
            linestyle="--",
            linewidth=1,
            label="+1 sigma",
        )
        price_ax.plot(
            data.index,
            trend - residual_std,
            color=RED,
            linestyle="--",
            linewidth=1,
            label="-1 sigma",
        )
        price_ax.plot(data.index, data["ema21"], color=CYAN, linewidth=1.1, label="EMA 21")
        price_ax.plot(data.index, data["ema50"], color=VIOLET, linewidth=1.1, label="EMA 50")
        price_ax.plot(data.index, data["ema200"], color=ORANGE, linewidth=1.1, label="EMA 200")
        style_legend(price_ax, ncols=2)

        style_axis(volume_ax, title="Volume", grid_axis="y")
        volume_colors = np.where(data["Close"] >= data["Open"], GREEN, RED)
        volume_ax.bar(data.index, data["Volume"], color=volume_colors, alpha=0.68, width=1.0)
        volume_ax.yaxis.set_major_formatter(
            FuncFormatter(lambda value, _position: f"{value / 1_000_000:.0f}M")
        )
        fig.autofmt_xdate()
        fig.tight_layout(rect=(0, 0.05, 1, 0.97))
        add_terminal_footer(
            fig,
            left=f"{history.ticker} trend diagnostics",
            right=f"source {history.provider}",
        )
        return image_from_figure(fig, f"{history.ticker.lower()}-regression.png"), {
            "slope_per_day": float(slope),
            "residual_std": residual_std,
        }


def render_portfolio_chart(
    histories: list[HistoryResult],
    *,
    investment_per_stock: float,
    benchmark: HistoryResult | None = None,
) -> tuple[RenderedImage, dict[str, Any]]:
    apply_terminal_style()
    fig, ax = plt.subplots(figsize=(15, 8))
    with managed_figure(fig):
        style_axis(
            ax,
            title="Portfolio equity curve",
            subtitle=f"{len(histories)} holdings | USD {investment_per_stock:,.0f} per stock",
        )
        combined = pd.DataFrame()
        final_values: dict[str, float] = {}
        line_colors = [CYAN, VIOLET, GREEN, RED, ORANGE, "#8ef6d1", "#d7a5ff"]

        for index, history in enumerate(histories):
            prices = history.data["Adj Close"].dropna()
            normalized = prices / prices.iloc[0] * investment_per_stock
            combined[history.ticker] = normalized
            ax.plot(
                normalized.index,
                normalized,
                color=line_colors[index % len(line_colors)],
                linewidth=1.65,
                alpha=0.58,
                label=history.ticker,
            )
            final_values[history.ticker] = float(normalized.iloc[-1])

        combined["Portfolio"] = combined.sum(axis=1, min_count=1)
        portfolio = combined["Portfolio"].dropna()
        benchmark_series = normalized_benchmark_series(
            benchmark, portfolio, investment_per_stock, histories
        )
        if benchmark is not None and benchmark_series is not None:
            ax.plot(
                benchmark_series.index,
                benchmark_series,
                color=CYAN,
                linestyle="--",
                linewidth=2.4,
                label=f"{benchmark.ticker} benchmark",
            )

        ax.fill_between(
            portfolio.index,
            float(portfolio.min()) * 0.985,
            portfolio,
            color=AMBER,
            alpha=0.075,
        )
        ax.plot(
            portfolio.index,
            portfolio,
            color=AMBER_HOT,
            linewidth=3.2,
            label="Portfolio",
            path_effects=glow_effect(5.0),
        )
        ax.set_ylabel("Value")
        format_currency_y_axis(ax)
        style_legend(ax, ncols=2)
        total_return = series_return(portfolio)
        max_drawdown = series_max_drawdown(portfolio)
        volatility = series_annualized_volatility(portfolio)
        fig.autofmt_xdate()
        fig.tight_layout(rect=(0, 0.05, 1, 0.96))
        add_terminal_footer(
            fig,
            left=(
                f"Return {total_return * 100:.1f}% | "
                f"Drawdown {max_drawdown * 100:.1f}% | "
                f"Vol {volatility * 100:.1f}%"
            ),
            right="portfolio scanner",
        )

        meta: dict[str, Any] = {
            "final_values": final_values,
            "initial_value": float(portfolio.iloc[0]),
            "portfolio_final": float(portfolio.iloc[-1]),
            "total_return": total_return,
            "max_drawdown": max_drawdown,
            "annualized_volatility": volatility,
            "equity_curve": series_points(portfolio),
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

        return image_from_figure(fig, "portfolio-performance.png"), meta


def normalized_benchmark_series(
    benchmark: HistoryResult | None,
    portfolio: pd.Series,
    investment_per_stock: float,
    histories: list[HistoryResult],
) -> pd.Series | None:
    if benchmark is None or portfolio.empty:
        return None

    prices = benchmark.data["Adj Close"].dropna()
    if prices.empty:
        return None

    initial_capital = investment_per_stock * len(histories)
    return prices / prices.iloc[0] * initial_capital


def series_return(series: pd.Series) -> float:
    clean = series.dropna()
    if len(clean) < 2 or float(clean.iloc[0]) == 0:
        return 0.0
    return float(clean.iloc[-1] / clean.iloc[0] - 1)


def series_max_drawdown(series: pd.Series) -> float:
    clean = series.dropna()
    if clean.empty:
        return 0.0
    drawdowns = clean / clean.cummax() - 1
    return float(drawdowns.min())


def series_annualized_volatility(series: pd.Series) -> float:
    returns = series.dropna().pct_change().dropna()
    if returns.empty:
        return 0.0
    return float(returns.std() * np.sqrt(252))


def series_points(series: pd.Series, *, interval: str = "1d") -> list[dict[str, float | str]]:
    return [
        {"date": series_timestamp_label(timestamp, interval), "value": float(value)}
        for timestamp, value in series.dropna().items()
    ]


def render_volatility_chart(histories: list[HistoryResult]) -> tuple[RenderedImage, dict[str, Any]]:
    apply_terminal_style()
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
    labels = [str(row["ticker"]) for row in rows]
    annual_vols = [float(row["annual_vol"]) * 100 for row in rows]
    fig, ax = plt.subplots(figsize=(12, 7))
    with managed_figure(fig):
        style_axis(
            ax,
            title="Historical volatility radar",
            subtitle="Annualized realized volatility with expected ranges",
            grid_axis="x",
        )
        y_positions = np.arange(len(labels))
        colors = [AMBER, GREEN, CYAN, RED, VIOLET, ORANGE, "#8ef6d1"][: len(labels)]
        bars = ax.barh(
            y_positions,
            annual_vols,
            color=colors,
            alpha=0.86,
            edgecolor=TEXT,
            linewidth=0.35,
        )
        ax.set_yticks(y_positions, labels=labels)
        ax.invert_yaxis()
        ax.set_xlabel("Annualized volatility (%)")
        for bar, row, value in zip(bars, rows, annual_vols, strict=False):
            ax.text(
                bar.get_width() + max(annual_vols, default=1.0) * 0.015,
                bar.get_y() + bar.get_height() / 2,
                (
                    f"{value:.1f}%  |  "
                    f"1w +/- {float(row['one_week_range']):.2f}  |  "
                    f"1m +/- {float(row['one_month_range']):.2f}"
                ),
                ha="left",
                va="center",
                color=TEXT,
                fontsize=9,
                fontweight="bold",
            )
        ax.set_xlim(0, max(annual_vols, default=1.0) * 1.35)
        fig.tight_layout(rect=(0, 0.06, 1, 0.96))
        add_terminal_footer(fig, left="volatility scanner", right=f"{len(rows)} symbols")
        return image_from_figure(fig, "volatility.png"), {"rows": rows}


def render_ridge_growth_chart(
    history: HistoryResult, *, period: str, config: RidgeGrowthConfig | None = None
) -> tuple[RenderedImage, dict[str, Any]]:
    apply_terminal_style()
    data, meta = calculate_ridge_growth_strategy(history, config)
    _flow_frame, flow_meta = calculate_flow_compass_indicator(history)
    auction_meta = calculate_auction_observation(history)
    meta = {
        **meta,
        "period": period,
        "flow_compass": flow_meta,
        "auction": auction_meta,
    }

    fig, (price_ax, equity_ax, table_ax) = plt.subplots(
        3, 1, figsize=(15, 11), gridspec_kw={"height_ratios": [3.4, 1.0, 1.15]}
    )
    with managed_figure(fig):
        style_axis(
            price_ax,
            title=f"{history.ticker} ridge core growth control",
            subtitle=(
                f"1D {period.upper()} | state {meta['state']} | recommendation "
                f"{meta['recommendation']}"
            ),
        )
        price_ax.plot(
            data.index,
            data["Close"],
            color=TEXT_STRONG,
            linewidth=1.35,
            label="Close",
            path_effects=glow_effect(4.0),
        )
        price_ax.plot(data.index, data["fast_ma"], color=CYAN, linewidth=1.8, label="Fast EMA 75")
        price_ax.plot(data.index, data["base_ma"], color=AMBER, linewidth=1.8, label="Base EMA 150")
        price_ax.plot(
            data.index, data["major_ma"], color=MUTED, linewidth=1.6, label="Major SMA 200"
        )

        close_min = float(data["Close"].min())
        close_max = float(data["Close"].max())
        price_ax.fill_between(
            data.index,
            close_min * 0.985,
            close_max * 1.015,
            where=data["in_trade"].to_numpy(dtype=bool),
            color=GREEN,
            alpha=0.055,
            label="Long exposure",
        )
        buy_rows = data[data["buy_signal"]]
        sell_rows = data[data["sell_signal"]]
        price_ax.scatter(
            buy_rows.index,
            buy_rows["Low"] * 0.985,
            marker="^",
            s=94,
            color=GREEN,
            edgecolors=CHART_BG,
            linewidths=0.8,
            label="Buy",
            zorder=5,
        )
        price_ax.scatter(
            sell_rows.index,
            sell_rows["High"] * 1.015,
            marker="v",
            s=94,
            color=RED,
            edgecolors=CHART_BG,
            linewidths=0.8,
            label="Sell",
            zorder=5,
        )
        price_ax.axhline(
            float(auction_meta["poc"]),
            color=AMBER_HOT,
            linestyle="-.",
            linewidth=1.6,
            alpha=0.78,
            label=f"POC {float(auction_meta['poc']):.2f}",
        )
        price_ax.axhspan(
            float(auction_meta["val"]),
            float(auction_meta["vah"]),
            color=AMBER,
            alpha=0.04,
        )
        style_legend(price_ax, ncols=3)
        price_ax.set_ylabel("Price")

        style_axis(equity_ax, title="Strategy equity", grid_axis="y")
        equity_ax.plot(
            data.index,
            data["equity"],
            color=GREEN if float(meta["total_return"]) >= 0 else RED,
            linewidth=2.2,
            path_effects=glow_effect(4.0),
        )
        equity_ax.axhline(
            RidgeGrowthConfig().initial_capital, color=MUTED, linestyle="--", alpha=0.62
        )
        equity_ax.set_ylabel("Equity")
        format_currency_y_axis(equity_ax)

        table_ax.axis("off")
        table_rows = [
            ("State", str(meta["state"])),
            ("Recommendation", str(meta["recommendation"])),
            ("Equity", f"${float(meta['ending_equity']):,.0f}"),
            ("Return", f"{float(meta['total_return']) * 100:.1f}%"),
            ("Drawdown", f"{float(meta['max_drawdown']) * 100:.1f}%"),
            ("Flow", f"{flow_meta['state']} {float(flow_meta['score'] or 0):.1f}"),
            ("AMT", f"{auction_meta['location']} / POC {float(auction_meta['poc']):.2f}"),
            ("Caveat", "persistent large-cap trend bias"),
        ]
        table = table_ax.table(
            cellText=table_rows,
            colLabels=["Ridge Growth", "Dashboard"],
            colLoc="left",
            cellLoc="left",
            loc="center",
        )
        table.auto_set_font_size(False)
        table.set_fontsize(8.5)
        table.scale(1, 1.35)
        for (row, _col), cell in table.get_celld().items():
            cell.set_edgecolor(AMBER)
            cell.set_linewidth(0.55)
            cell.set_facecolor(PANEL if row else GREEN if meta["state"] == "LONG" else AX_BG)
            cell.get_text().set_color(TEXT_STRONG if row == 0 else TEXT)

        fig.autofmt_xdate()
        fig.tight_layout(rect=(0, 0.05, 1, 0.97))
        add_terminal_footer(
            fig,
            left=(
                f"{history.ticker} {period} | buys {meta['buy_count']} | sells "
                f"{meta['sell_count']} | flow {flow_meta['state']}"
            ),
            right="ridge growth",
        )
        return image_from_figure(fig, f"{history.ticker.lower()}-ridge-growth-{period}.png"), meta


def render_flow_compass_chart(
    history: HistoryResult, *, period: str, config: FlowCompassConfig | None = None
) -> tuple[RenderedImage, dict[str, Any]]:
    apply_terminal_style()
    data, meta = calculate_flow_compass_indicator(history, config)
    cfg = config or FlowCompassConfig()
    fig, (price_ax, score_ax, component_ax) = plt.subplots(
        3, 1, figsize=(15, 10), gridspec_kw={"height_ratios": [1.45, 2.4, 1.25]}
    )
    with managed_figure(fig):
        style_axis(
            price_ax,
            title=f"{history.ticker} flow compass dashboard",
            subtitle=f"1D {period.upper()} | {meta['state']} | delta via daily signed-volume proxy",
        )
        price_ax.plot(
            data.index,
            data["Close"],
            color=TEXT_STRONG,
            linewidth=1.35,
            path_effects=glow_effect(4.0),
            label="Close",
        )
        long_rows = data[data["fresh_long"]]
        short_rows = data[data["fresh_short"]]
        price_ax.scatter(
            long_rows.index,
            long_rows["Low"] * 0.985,
            marker="^",
            s=78,
            color=GREEN,
            edgecolors=CHART_BG,
            linewidths=0.7,
            label="Fresh long",
        )
        price_ax.scatter(
            short_rows.index,
            short_rows["High"] * 1.015,
            marker="v",
            s=78,
            color=RED,
            edgecolors=CHART_BG,
            linewidths=0.7,
            label="Fresh short",
        )
        style_legend(price_ax, ncols=3)
        price_ax.set_ylabel("Price")

        style_axis(score_ax, title="Main bias score", grid_axis="y")
        histogram_colors = [
            GREEN if value > cfg.trigger_level else RED if value < -cfg.trigger_level else MUTED
            for value in data["flow_score"]
        ]
        score_ax.bar(data.index, data["flow_score"], color=histogram_colors, alpha=0.72, width=1.0)
        score_ax.plot(
            data.index,
            data["compass_signal"],
            color=AMBER_HOT,
            linewidth=2.1,
            label="Compass signal",
            path_effects=glow_effect(3.2),
        )
        score_ax.axhline(0, color=TEXT, linewidth=0.8, alpha=0.72)
        score_ax.axhline(cfg.trigger_level, color=GREEN, linestyle="--", linewidth=1.1, alpha=0.72)
        score_ax.axhline(-cfg.trigger_level, color=RED, linestyle="--", linewidth=1.1, alpha=0.72)
        score_ax.axhline(cfg.strong_level, color=CYAN, linestyle=":", linewidth=1.35, alpha=0.78)
        score_ax.axhline(-cfg.strong_level, color=ORANGE, linestyle=":", linewidth=1.35, alpha=0.78)
        score_ax.scatter(
            long_rows.index,
            long_rows["flow_score"],
            marker="^",
            s=80,
            color=GREEN,
            edgecolors=CHART_BG,
            linewidths=0.7,
            zorder=5,
            label="Long trigger",
        )
        score_ax.scatter(
            short_rows.index,
            short_rows["flow_score"],
            marker="v",
            s=80,
            color=RED,
            edgecolors=CHART_BG,
            linewidths=0.7,
            zorder=5,
            label="Short trigger",
        )
        score_ax.set_ylim(-105, 105)
        score_ax.set_ylabel("Score")
        style_legend(score_ax, ncols=3)

        style_axis(component_ax, title="Component scores", grid_axis="x")
        components = [
            ("Volume", safe_float(meta["volume_score"])),
            ("Trend", safe_float(meta["trend_score"])),
            ("Momentum", safe_float(meta["momentum_score"])),
            ("Value", safe_float(meta["value_score"])),
            ("RVI", safe_float(meta["rvi_score"])),
        ]
        labels = [name for name, _score in components]
        scores = [score or 0.0 for _name, score in components]
        y_positions = np.arange(len(labels))
        bars = component_ax.barh(
            y_positions,
            scores,
            color=[component_color(score) for _name, score in components],
            alpha=0.86,
            edgecolor=TEXT,
            linewidth=0.35,
        )
        component_ax.axvline(0, color=TEXT, linewidth=0.9, alpha=0.72)
        component_ax.set_xlim(-105, 105)
        component_ax.set_yticks(y_positions, labels=labels)
        component_ax.invert_yaxis()
        for bar, score in zip(bars, scores, strict=False):
            component_ax.text(
                score + (3 if score >= 0 else -3),
                bar.get_y() + bar.get_height() / 2,
                f"{score:.1f}",
                ha="left" if score >= 0 else "right",
                va="center",
                color=TEXT_STRONG,
                fontsize=9,
                fontweight="bold",
            )

        fig.autofmt_xdate()
        fig.tight_layout(rect=(0, 0.05, 1, 0.97))
        add_terminal_footer(
            fig,
            left=f"{history.ticker} flow {float(meta['score'] or 0):.1f} | {meta['state']}",
            right="flow compass",
        )
        meta = {**meta, "period": period}
        return image_from_figure(fig, f"{history.ticker.lower()}-flow-compass-{period}.png"), meta
