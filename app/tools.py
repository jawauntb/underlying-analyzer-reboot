from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
import yfinance as yf

from app.analysis import summarize_stock
from app.charts import (
    RenderedImage,
    apply_terminal_style,
    calculate_auction_levels,
    image_from_figure,
)
from app.market_data import HistoryResult, MarketDataClient, MarketDataError, clean_ticker

PIXEL_STYLE = (
    "Create a depiction in 8-bit, pixelated, retro video game style with crisp graphics, "
    "esoteric market symbols, vibrant colors, and no visible text of:"
)


def build_stock_fax(client: MarketDataClient, ticker: str) -> dict[str, Any]:
    symbol = clean_ticker(ticker)
    history = client.get_history(symbol, period="2y")
    summary = summarize_stock(client, symbol)
    data = history.data
    close = data["Adj Close"].dropna()
    returns = close.pct_change().dropna()
    vah, val, poc = calculate_auction_levels(data)

    return {
        "Ticker": symbol,
        "Name": summary["name"],
        "Sector": summary["sector"],
        "Industry": summary["industry"],
        "Provider": history.provider,
        "Provider Note": history.note,
        "Snapshot": {
            "Price": summary["price"],
            "Change": summary["change"],
            "Change (%)": summary["change_percent"],
            "Market Cap": summary["market_cap"],
            "Trailing PE": summary["trailing_pe"],
            "Forward PE": summary["forward_pe"],
            "Beta": summary["beta"],
            "52W High": summary["fifty_two_week_high"],
            "52W Low": summary["fifty_two_week_low"],
        },
        "Volatility Metrics": volatility_metrics(close),
        "Regression Trend": regression_trend(close),
        "EMAs Summary": ema_summary(close),
        "Auction Market Theory Price Levels": {
            "Point of Control (POC)": poc,
            "Value Area High (VAH)": vah,
            "Value Area Low (VAL)": val,
        },
        "Signal Summary": signal_summary(close, returns, poc),
        "Export Rows": price_rows(history),
    }


def volatility_metrics(close: pd.Series) -> dict[str, dict[str, float]]:
    windows = {"1M": 21, "3M": 63, "6M": 126, "1Y": 252}
    returns = close.pct_change().dropna()
    metrics: dict[str, dict[str, float]] = {}
    for label, window in windows.items():
        window_returns = returns.tail(window)
        if window_returns.empty:
            metrics[label] = {"Historical Volatility": 0.0, "Average Daily Move": 0.0}
            continue
        metrics[label] = {
            "Historical Volatility": float(window_returns.std() * np.sqrt(252)),
            "Average Daily Move": float(window_returns.abs().mean()),
        }
    return metrics


def regression_trend(close: pd.Series) -> dict[str, float | str]:
    clean = close.dropna()
    if len(clean) < 3:
        return {
            "Trend Slope": 0.0,
            "Trend Direction": "Flat",
            "Upper Bound (+1 Std Dev)": float(clean.iloc[-1]) if not clean.empty else 0.0,
            "Lower Bound (-1 Std Dev)": float(clean.iloc[-1]) if not clean.empty else 0.0,
        }

    x_values = np.arange(len(clean))
    y_values = clean.to_numpy(dtype=float)
    slope, intercept = np.polyfit(x_values, y_values, 1)
    trend = slope * x_values + intercept
    residual_std = float(np.std(y_values - trend))
    direction = "Bullish" if slope > 0 else "Bearish" if slope < 0 else "Flat"
    return {
        "Trend Slope": float(slope),
        "Trend Direction": direction,
        "Upper Bound (+1 Std Dev)": float(trend[-1] + residual_std),
        "Lower Bound (-1 Std Dev)": float(trend[-1] - residual_std),
    }


def ema_summary(close: pd.Series) -> dict[str, Any]:
    clean = close.dropna()
    ema21 = clean.ewm(span=21, adjust=False).mean()
    ema55 = clean.ewm(span=55, adjust=False).mean()
    crossovers = []
    if len(clean) >= 2:
        spread = ema21 - ema55
        previous = spread.shift(1)
        events = spread[(spread > 0) & (previous <= 0) | (spread < 0) & (previous >= 0)].tail(5)
        for timestamp, value in events.items():
            crossovers.append(
                {
                    "Date": timestamp.date().isoformat(),
                    "Type": "Bullish" if value > 0 else "Bearish",
                }
            )
    return {
        "Latest EMA21": float(ema21.iloc[-1]) if not ema21.empty else 0.0,
        "Latest EMA55": float(ema55.iloc[-1]) if not ema55.empty else 0.0,
        "Recent EMA Crossover Events": crossovers,
    }


def signal_summary(close: pd.Series, returns: pd.Series, poc: float) -> dict[str, float | str]:
    latest = float(close.iloc[-1])
    trend_50d = float(latest / close.tail(50).mean() - 1) if len(close) >= 50 else 0.0
    annual_vol = float(returns.std() * np.sqrt(252)) if not returns.empty else 0.0
    setup = "Momentum" if trend_50d > 0.03 else "Mean reversion" if trend_50d < -0.03 else "Balance"
    return {
        "Setup": setup,
        "50D Trend (%)": trend_50d * 100,
        "Annual Volatility (%)": annual_vol * 100,
        "Distance From POC (%)": ((latest / poc) - 1) * 100 if poc else 0.0,
    }


def price_rows(history: HistoryResult) -> list[dict[str, float | str]]:
    rows = []
    for timestamp, row in history.data.tail(252).iterrows():
        rows.append(
            {
                "date": timestamp.date().isoformat(),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]),
            }
        )
    return rows


def build_market_memo(client: MarketDataClient, ticker: str) -> dict[str, Any]:
    report = build_stock_fax(client, ticker)
    snapshot = report["Snapshot"]
    signal = report["Signal Summary"]
    trend = report["Regression Trend"]
    auction = report["Auction Market Theory Price Levels"]
    memo = (
        f"### {report['Ticker']} Vision\n\n"
        f"**Name:** {report['Name']}\n\n"
        f"**Setup:** {signal['Setup']} with a {signal['50D Trend (%)']:.2f}% 50-day trend "
        f"and {signal['Annual Volatility (%)']:.2f}% annualized volatility.\n\n"
        f"**Price Map:** Latest price is {snapshot['Price']:.2f}. Auction levels put POC at "
        f"{auction['Point of Control (POC)']:.2f}, VAH at {auction['Value Area High (VAH)']:.2f}, "
        f"and VAL at {auction['Value Area Low (VAL)']:.2f}.\n\n"
        f"**Trend:** Regression direction reads {trend['Trend Direction']} with slope "
        f"{trend['Trend Slope']:.4f} per trading day.\n\n"
        f"**Watch:** Respect the 52-week range from {snapshot['52W Low']:.2f} to "
        f"{snapshot['52W High']:.2f}, and compare any fresh move against volume and the POC."
    )
    return {"Ticker": report["Ticker"], "Market Memo": memo, "Report": report}


def render_moneyline_chart(
    ticker: str, expiry: str | None = None
) -> tuple[RenderedImage, dict[str, Any]]:
    symbol = clean_ticker(ticker)
    stock = yf.Ticker(symbol)
    history = stock.history(period="5d")
    if history.empty:
        raise MarketDataError(f"No recent price data for {symbol}")

    current_price = float(history["Close"].dropna().iloc[-1])
    selected_expiry = choose_expiry(stock, expiry)
    option_chain = stock.option_chain(selected_expiry)
    rows = option_rows(option_chain.calls, option_chain.puts, current_price)
    if not rows:
        raise MarketDataError(f"No usable option rows for {symbol} {selected_expiry}")

    image = moneyline_image(symbol, selected_expiry, current_price, rows)
    return image, {
        "ticker": symbol,
        "expiry": selected_expiry,
        "current_price": current_price,
        "rows": rows,
    }


def choose_expiry(stock: yf.Ticker, expiry: str | None) -> str:
    expiries = list(stock.options)
    if not expiries:
        raise MarketDataError("No listed options were returned")
    if expiry and expiry in expiries:
        return expiry

    target = parse_expiry(expiry) if expiry else next_friday()
    dated = [(datetime.strptime(value, "%Y-%m-%d").date(), value) for value in expiries]
    dated.sort(key=lambda item: abs((item[0] - target).days))
    return dated[0][1]


def parse_expiry(expiry: str | None) -> date:
    if not expiry:
        return next_friday()
    return datetime.strptime(expiry, "%Y-%m-%d").date()


def next_friday() -> date:
    today = datetime.now().date()
    days_until_friday = (4 - today.weekday()) % 7 or 7
    return today + timedelta(days=days_until_friday)


def option_rows(
    calls: pd.DataFrame, puts: pd.DataFrame, current_price: float
) -> list[dict[str, Any]]:
    nearby = pd.concat([calls[["strike"]], puts[["strike"]]], ignore_index=True).drop_duplicates()
    if nearby.empty:
        return []
    strikes = (
        nearby.assign(distance=(nearby["strike"] - current_price).abs())
        .sort_values("distance")
        .head(9)["strike"]
        .sort_values()
        .tolist()
    )
    rows = []
    for strike in strikes:
        call = first_option_at_strike(calls, strike)
        put = first_option_at_strike(puts, strike)
        call_oi = option_number(call, "openInterest")
        put_oi = option_number(put, "openInterest")
        call_last = option_number(call, "lastPrice")
        put_last = option_number(put, "lastPrice")
        rows.append(
            {
                "strike": float(strike),
                "call_open_interest": call_oi,
                "put_open_interest": put_oi,
                "call_last": call_last,
                "put_last": put_last,
                "net_open_interest": call_oi - put_oi,
                "put_call_ratio": put_oi / call_oi if call_oi else 0.0,
            }
        )
    return rows


def first_option_at_strike(options: pd.DataFrame, strike: float) -> pd.Series | None:
    matches = options[options["strike"] == strike]
    if matches.empty:
        return None
    return matches.iloc[0]


def option_number(row: pd.Series | None, column: str) -> float:
    if row is None:
        return 0.0
    value = row.get(column)
    if value is None or pd.isna(value):
        return 0.0
    return float(value)


def moneyline_image(
    ticker: str, expiry: str, current_price: float, rows: list[dict[str, Any]]
) -> RenderedImage:
    apply_terminal_style()
    fig, (chart_ax, table_ax) = plt.subplots(
        1, 2, figsize=(15, 8), gridspec_kw={"width_ratios": [2.2, 1]}
    )
    strikes = [row["strike"] for row in rows]
    call_oi = [row["call_open_interest"] for row in rows]
    put_oi = [-row["put_open_interest"] for row in rows]
    chart_ax.bar(strikes, call_oi, width=1.2, color="#83ff8f", label="Call OI")
    chart_ax.bar(strikes, put_oi, width=1.2, color="#ff6b5f", label="Put OI")
    chart_ax.axvline(current_price, color="#ffcc4d", linewidth=2, label=f"Spot {current_price:.2f}")
    chart_ax.axhline(0, color="#f7f3cf", linewidth=1)
    chart_ax.set_title(f"{ticker} moneyline - {expiry}")
    chart_ax.set_xlabel("Strike")
    chart_ax.set_ylabel("Open interest")
    chart_ax.legend(loc="upper left")
    chart_ax.grid(True, axis="y")

    table_ax.axis("off")
    table_rows = [
        [
            f"{row['strike']:.0f}",
            f"{row['call_open_interest']:.0f}",
            f"{row['put_open_interest']:.0f}",
            f"{row['put_call_ratio']:.2f}",
        ]
        for row in rows
    ]
    table = table_ax.table(
        cellText=table_rows,
        colLabels=["Strike", "Call OI", "Put OI", "P/C"],
        cellLoc="center",
        loc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    fig.tight_layout()
    return image_from_figure(fig, f"{ticker.lower()}-moneyline.png")


def generate_pixel_image(
    prompt: str,
    *,
    api_key: str | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    clean_prompt = prompt.strip()
    if not clean_prompt:
        raise ValueError("Prompt is required")

    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key:
        raise MarketDataError("OPENAI_API_KEY is not configured for Pixel generation")

    http = session or requests.Session()
    response = http.post(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": "gpt-image-1-mini",
            "prompt": f"{PIXEL_STYLE} {clean_prompt}",
            "size": "1024x1024",
            "quality": "low",
            "n": 1,
        },
        timeout=120,
    )
    if response.status_code >= 400:
        raise MarketDataError(openai_error_text(response))

    payload = response.json()
    data = payload.get("data") or []
    image = data[0] if data else {}
    b64_json = image.get("b64_json")
    if not b64_json:
        raise MarketDataError("OpenAI image response did not include image data")

    return {
        "created": payload.get("created") or int(time.time()),
        "image": {"data": b64_json, "mime": "image/png", "filename": "pixel.png"},
        "prompt": clean_prompt,
        "styled_prompt": f"{PIXEL_STYLE} {clean_prompt}",
    }


def openai_error_text(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text or "OpenAI image request failed"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    return "OpenAI image request failed"
