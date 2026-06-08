from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from app.market_data import HistoryResult


@dataclass(frozen=True)
class RenderedImage:
    data: str
    mime: str
    filename: str


def image_from_figure(fig: plt.Figure, filename: str) -> RenderedImage:
    buffer = BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight", facecolor=fig.get_facecolor(), dpi=150)
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
            "figure.facecolor": "#07111f",
            "axes.facecolor": "#07111f",
            "axes.edgecolor": "#e9c949",
            "axes.labelcolor": "#f6d94c",
            "axes.titlecolor": "#f6d94c",
            "xtick.color": "#8df7a3",
            "ytick.color": "#8df7a3",
            "grid.color": "#31558a",
            "grid.linestyle": "--",
            "grid.linewidth": 0.6,
            "legend.facecolor": "#0d1f35",
            "legend.edgecolor": "#f6d94c",
            "legend.labelcolor": "#f6d94c",
            "font.family": "DejaVu Sans",
        }
    )


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
    ax.plot(
        data.index, data["Close"], color="white", linewidth=1.4, label=f"{history.ticker} close"
    )

    candle_width = max(0.25, min(0.8, 12 / max(len(data), 1)))
    for timestamp, row in data.iterrows():
        color = "#87ff7b" if row["Close"] >= row["Open"] else "#ff6b5f"
        x_value = mdates.date2num(timestamp)
        ax.vlines(x_value, row["Low"], row["High"], color=color, linewidth=0.9, alpha=0.8)
        ax.vlines(
            x_value, row["Open"], row["Close"], color=color, linewidth=candle_width * 4, alpha=0.95
        )

    ax.axhline(vah, color="#87ff7b", linestyle="--", linewidth=2, label=f"VAH {vah:.2f}")
    ax.axhline(val, color="#ff6b5f", linestyle="--", linewidth=2, label=f"VAL {val:.2f}")
    ax.axhline(poc, color="#ffd84d", linestyle="-.", linewidth=2.5, label=f"POC {poc:.2f}")
    ax.fill_between(data.index, poc, vah, color="#37e07a", alpha=0.12)
    ax.fill_between(data.index, val, poc, color="#ff5a66", alpha=0.12)
    ax.set_title(f"{history.ticker} auction levels - {period}")
    ax.set_xlabel("Date")
    ax.set_ylabel("Price")
    ax.grid(True)
    ax.legend(loc="upper left")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    fig.autofmt_xdate()
    fig.text(
        0.5,
        0.02,
        f"Fair price {poc:.2f} | high {vah:.2f} | low {val:.2f} | source {history.provider}",
        ha="center",
        color="#ffd84d",
        fontsize=11,
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
    image = ax.imshow(values, cmap="Spectral", aspect="auto", vmin=-vmax, vmax=vmax)
    ax.set_title(f"{history.ticker} monthly performance map")
    ax.set_yticks(np.arange(len(labels)), labels=labels)
    ax.set_xticks(
        np.arange(len(table.columns)),
        labels=[str(column) for column in table.columns],
        rotation=45,
        ha="right",
    )
    for row_index in range(values.shape[0]):
        for column_index in range(values.shape[1]):
            value = table.iloc[row_index, column_index]
            if pd.notna(value):
                ax.text(
                    column_index,
                    row_index,
                    f"{value:.1f}",
                    ha="center",
                    va="center",
                    color="#07111f",
                    fontsize=8,
                )
    fig.colorbar(image, ax=ax, label="Monthly return (%)")
    fig.tight_layout()
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
    price_ax.plot(data.index, data["Close"], color="white", linewidth=1.4, label="Close")
    price_ax.plot(data.index, trend, color="#ffd84d", linewidth=2, label="Regression")
    price_ax.plot(
        data.index,
        trend + residual_std,
        color="#87ff7b",
        linestyle="--",
        linewidth=1,
        label="+1 sigma",
    )
    price_ax.plot(
        data.index,
        trend - residual_std,
        color="#ff6b5f",
        linestyle="--",
        linewidth=1,
        label="-1 sigma",
    )
    price_ax.plot(data.index, data["ema21"], color="#62d6ff", linewidth=1, label="EMA 21")
    price_ax.plot(data.index, data["ema50"], color="#cb8cff", linewidth=1, label="EMA 50")
    price_ax.plot(data.index, data["ema200"], color="#ffae57", linewidth=1, label="EMA 200")
    price_ax.set_title(f"{history.ticker} regression and EMAs")
    price_ax.grid(True)
    price_ax.legend(loc="upper left", ncols=2)

    volume_ax.bar(data.index, data["Volume"], color="#37e07a", alpha=0.65)
    volume_ax.set_title("Volume")
    volume_ax.grid(True)
    fig.autofmt_xdate()
    fig.tight_layout()
    return image_from_figure(fig, f"{history.ticker.lower()}-regression.png"), {
        "slope_per_day": float(slope),
        "residual_std": residual_std,
    }


def render_portfolio_chart(
    histories: list[HistoryResult], *, investment_per_stock: float
) -> tuple[RenderedImage, dict[str, Any]]:
    apply_terminal_style()
    fig, ax = plt.subplots(figsize=(15, 8))
    combined = pd.DataFrame()
    final_values: dict[str, float] = {}

    for history in histories:
        prices = history.data["Adj Close"].dropna()
        normalized = prices / prices.iloc[0] * investment_per_stock
        combined[history.ticker] = normalized
        ax.plot(normalized.index, normalized, linewidth=2, label=history.ticker)
        final_values[history.ticker] = float(normalized.iloc[-1])

    combined["Portfolio"] = combined.sum(axis=1)
    ax.plot(combined.index, combined["Portfolio"], color="#ffd84d", linewidth=3, label="Portfolio")
    ax.set_title("Portfolio cash performance")
    ax.set_ylabel("Value")
    ax.grid(True)
    ax.legend(loc="upper left")
    fig.autofmt_xdate()
    fig.tight_layout()
    return image_from_figure(fig, "portfolio-performance.png"), {
        "final_values": final_values,
        "portfolio_final": float(combined["Portfolio"].dropna().iloc[-1]),
    }


def render_volatility_chart(histories: list[HistoryResult]) -> tuple[RenderedImage, dict[str, Any]]:
    apply_terminal_style()
    rows: list[dict[str, float | str]] = []
    for history in histories:
        close = history.data["Adj Close"].dropna()
        returns = close.pct_change().dropna()
        annual_vol = float(returns.std() * np.sqrt(252))
        price = float(close.iloc[-1])
        rows.append(
            {
                "ticker": history.ticker,
                "price": price,
                "daily_vol": float(returns.std()),
                "annual_vol": annual_vol,
                "one_week_range": price * annual_vol * np.sqrt(5 / 252),
                "one_month_range": price * annual_vol * np.sqrt(21 / 252),
            }
        )

    labels = [str(row["ticker"]) for row in rows]
    annual_vols = [float(row["annual_vol"]) * 100 for row in rows]
    fig, ax = plt.subplots(figsize=(12, 7))
    bars = ax.bar(
        labels,
        annual_vols,
        color=["#ffd84d", "#37e07a", "#62d6ff", "#ff6b5f", "#cb8cff"][: len(labels)],
    )
    ax.set_title("Historical volatility")
    ax.set_ylabel("Annualized volatility (%)")
    ax.grid(True, axis="y")
    for bar, value in zip(bars, annual_vols, strict=False):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height(),
            f"{value:.1f}%",
            ha="center",
            va="bottom",
            color="#f7f3cf",
        )
    fig.tight_layout()
    return image_from_figure(fig, "volatility.png"), {"rows": rows}
