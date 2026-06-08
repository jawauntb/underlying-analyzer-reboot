from __future__ import annotations

from typing import Any

import numpy as np

from app.market_data import HistoryResult, MarketDataClient, clean_ticker


def compact_number(value: Any) -> str:
    if not isinstance(value, int | float) or value != value:
        return "N/A"
    number = float(value)
    for suffix in ("", "K", "M", "B", "T"):
        if abs(number) < 1000:
            return f"{number:.2f}{suffix}"
        number /= 1000
    return f"{number:.2f}Q"


def summarize_stock(client: MarketDataClient, ticker: str) -> dict[str, Any]:
    symbol = clean_ticker(ticker)
    history: HistoryResult = client.get_history(symbol, period="2y")
    data = history.data
    close = data["Adj Close"].dropna()
    latest = float(close.iloc[-1])
    previous = float(close.iloc[-2]) if len(close) > 1 else latest
    returns = close.pct_change().dropna()
    profile = client.get_profile(symbol)

    annual_vol = float(returns.std() * np.sqrt(252)) if not returns.empty else 0.0
    trend_50 = float(close.iloc[-1] / close.tail(50).mean() - 1) if len(close) >= 50 else 0.0
    high_52 = float(close.tail(252).max())
    low_52 = float(close.tail(252).min())

    return {
        "ticker": symbol,
        "provider": history.provider,
        "provider_note": history.note,
        "name": profile.get("longName") or profile.get("shortName") or symbol,
        "sector": profile.get("sector") or "N/A",
        "industry": profile.get("industry") or "N/A",
        "price": latest,
        "change": latest - previous,
        "change_percent": ((latest - previous) / previous * 100) if previous else 0.0,
        "market_cap": compact_number(profile.get("marketCap")),
        "trailing_pe": profile.get("trailingPE") or "N/A",
        "forward_pe": profile.get("forwardPE") or "N/A",
        "beta": profile.get("beta") or "N/A",
        "annual_volatility": annual_vol,
        "trend_50d": trend_50,
        "fifty_two_week_high": high_52,
        "fifty_two_week_low": low_52,
    }


def build_scanner_rows(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [scanner_row(summary) for summary in summaries]
    rows.sort(key=lambda row: float(row["score"]), reverse=True)
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
    return rows


def scanner_row(summary: dict[str, Any]) -> dict[str, Any]:
    price = as_float(summary.get("price"))
    high_52 = as_float(summary.get("fifty_two_week_high"))
    low_52 = as_float(summary.get("fifty_two_week_low"))
    change_percent = as_float(summary.get("change_percent"))
    annual_volatility = as_float(summary.get("annual_volatility"))
    trend_50d = as_float(summary.get("trend_50d"))
    distance_from_52w_high = (price / high_52 - 1) if price and high_52 else 0.0
    distance_from_52w_low = (price / low_52 - 1) if price and low_52 else 0.0
    score = (
        trend_50d * 100
        + change_percent
        + distance_from_52w_high * 25
        - annual_volatility * 5
    )
    return {
        "rank": 0,
        "ticker": summary.get("ticker"),
        "name": summary.get("name"),
        "price": price,
        "change_percent": change_percent,
        "annual_volatility": annual_volatility,
        "trend_50d": trend_50d,
        "distance_from_52w_high": distance_from_52w_high,
        "distance_from_52w_low": distance_from_52w_low,
        "score": score,
    }


def as_float(value: Any) -> float:
    if isinstance(value, int | float):
        return float(value)
    return 0.0
