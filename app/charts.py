from __future__ import annotations

import base64
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

from app.market_data import HistoryResult

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


def apply_terminal_style() -> None:
    plt.rcParams.update(
        {
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
    )


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


def render_auction_chart(
    history: HistoryResult, *, period: str
) -> tuple[RenderedImage, dict[str, Any]]:
    apply_terminal_style()
    data = history.data
    vah, val, poc = calculate_auction_levels(data)
    fig, ax = plt.subplots(figsize=(15, 9))
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
            x_value, row["Open"], row["Close"], color=color, linewidth=candle_width * 4, alpha=0.95
        )

    ax.axhline(vah, color=GREEN, linestyle="--", linewidth=1.9, label=f"VAH {vah:.2f}")
    ax.axhline(val, color=RED, linestyle="--", linewidth=1.9, label=f"VAL {val:.2f}")
    ax.axhline(poc, color=AMBER_HOT, linestyle="-.", linewidth=2.4, label=f"POC {poc:.2f}")
    ax.axhspan(val, vah, color=AMBER, alpha=0.055)
    ax.fill_between(data.index, poc, vah, color=GREEN, alpha=0.11)
    ax.fill_between(data.index, val, poc, color=RED, alpha=0.11)
    label_last_value(ax, x_value=data.index[-1], y_value=vah, label=f"VAH {vah:.2f}", color=GREEN)
    label_last_value(ax, x_value=data.index[-1], y_value=poc, label=f"POC {poc:.2f}", color=AMBER)
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


def series_points(series: pd.Series) -> list[dict[str, float | str]]:
    return [
        {"date": timestamp.date().isoformat(), "value": float(value)}
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
