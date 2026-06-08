from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator, Mapping
from datetime import date, datetime, timedelta
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
import yfinance as yf

from app.analysis import (
    compact_number,
    missing_profile_value,
    profile_value,
    summarize_stock,
    trim_text,
)
from app.anthropic import AnthropicTextClient, GeneratedText, StreamingTextGenerator, TextGenerator
from app.charts import (
    AMBER,
    AX_BG,
    CHART_BG,
    CYAN,
    GREEN,
    MUTED,
    PANEL,
    RED,
    TEXT,
    TEXT_STRONG,
    RenderedImage,
    add_terminal_footer,
    apply_terminal_style,
    calculate_auction_levels,
    format_absolute_y_axis,
    image_from_figure,
    style_axis,
    style_legend,
)
from app.market_data import HistoryResult, MarketDataClient, MarketDataError, clean_ticker

PIXEL_STYLE = (
    "Create a depiction in 8-bit, pixelated, retro video game style with crisp graphics, "
    "esoteric market symbols, vibrant colors, and no visible text of:"
)
DEFAULT_OPENAI_IMAGE_MODEL = "gpt-image-2"
MARKET_TEXT_SYSTEM = (
    "You are The Underlying's institutional equity analyst. Produce sober, evidence-based "
    "research for a serious investor using only the provided structured data. Separate facts "
    "from inference, label missing information, and never invent prices, dates, news, "
    "earnings details, management commentary, catalysts, peers, or filings. Avoid hype, "
    "generic finance filler, and personalized investment advice."
)


def build_stock_fax(
    client: MarketDataClient,
    ticker: str,
    *,
    text_generator: TextGenerator | None = None,
    api_key: str | None = None,
    text_model: str | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    report = build_stock_fax_data(client, ticker)
    generated = generate_stock_fax_text(
        report,
        text_generator=text_generator,
        api_key=api_key,
        text_model=text_model,
        session=session,
    )
    return {
        **report,
        "Anthropic Report": generated.text,
        "Text Provider": generated.provider,
        "Text Model": generated.model,
    }


def build_stock_fax_data(client: MarketDataClient, ticker: str) -> dict[str, Any]:
    symbol = clean_ticker(ticker)
    history = client.get_history(symbol, period="2y")
    summary = summarize_stock(client, symbol)
    profile = client.get_profile(symbol)
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
        "Business Context": business_context(profile, summary),
        "Performance Metrics": performance_metrics(close),
        "Valuation Context": valuation_context(profile, summary),
        "Financial Quality": financial_quality(profile),
        "Analyst And Ownership Signals": analyst_ownership_signals(profile),
        "Management Snapshot": management_snapshot(profile),
        "Volatility Metrics": volatility_metrics(close),
        "Regression Trend": regression_trend(close),
        "EMAs Summary": ema_summary(close),
        "Auction Market Theory Price Levels": {
            "Point of Control (POC)": poc,
            "Value Area High (VAH)": vah,
            "Value Area Low (VAL)": val,
        },
        "Signal Summary": signal_summary(close, returns, poc),
        "Data Coverage": data_coverage(profile),
        "Export Rows": price_rows(history),
    }


def business_context(profile: Mapping[str, Any], summary: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "Company": summary.get("name"),
        "Sector": summary.get("sector"),
        "Industry": summary.get("industry"),
        "Country": profile_value(profile, "country"),
        "Website": profile_value(profile, "website"),
        "Employees": profile_value(profile, "fullTimeEmployees"),
        "Business Summary": trim_text(profile.get("longBusinessSummary"), limit=900),
    }


def performance_metrics(close: pd.Series) -> dict[str, dict[str, float] | float]:
    clean = close.dropna()
    if clean.empty:
        return {}

    latest = float(clean.iloc[-1])
    metrics: dict[str, dict[str, float] | float] = {
        "Latest Price": latest,
        "Distance From 52W High (%)": 0.0,
        "Distance From 52W Low (%)": 0.0,
    }
    high_52 = float(clean.tail(252).max())
    low_52 = float(clean.tail(252).min())
    if high_52:
        metrics["Distance From 52W High (%)"] = (latest / high_52 - 1) * 100
    if low_52:
        metrics["Distance From 52W Low (%)"] = (latest / low_52 - 1) * 100

    for label, window in {"1W": 5, "1M": 21, "3M": 63, "6M": 126, "1Y": 252, "2Y": 504}.items():
        if len(clean) < 2:
            continue
        start = float(clean.iloc[-window - 1]) if len(clean) > window else float(clean.iloc[0])
        metrics[label] = {
            "Return (%)": ((latest / start) - 1) * 100 if start else 0.0,
            "Start Price": start,
            "End Price": latest,
        }
    return metrics


def valuation_context(profile: Mapping[str, Any], summary: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "Market Cap": summary.get("market_cap"),
        "Enterprise Value": compact_profile_number(profile, "enterpriseValue"),
        "Trailing PE": summary.get("trailing_pe"),
        "Forward PE": summary.get("forward_pe"),
        "PEG Ratio": profile_value(profile, "pegRatio"),
        "Price To Sales": profile_value(profile, "priceToSalesTrailing12Months"),
        "Price To Book": profile_value(profile, "priceToBook"),
        "Enterprise To Revenue": profile_value(profile, "enterpriseToRevenue"),
        "Enterprise To EBITDA": profile_value(profile, "enterpriseToEbitda"),
        "Trailing EPS": profile_value(profile, "trailingEps"),
        "Forward EPS": profile_value(profile, "forwardEps"),
    }


def financial_quality(profile: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "Total Revenue": compact_profile_number(profile, "totalRevenue"),
        "Revenue Growth": profile_value(profile, "revenueGrowth"),
        "Gross Margins": profile_value(profile, "grossMargins"),
        "EBITDA Margins": profile_value(profile, "ebitdaMargins"),
        "Operating Margins": profile_value(profile, "operatingMargins"),
        "Profit Margins": profile_value(profile, "profitMargins"),
        "Return On Equity": profile_value(profile, "returnOnEquity"),
        "Free Cash Flow": compact_profile_number(profile, "freeCashflow"),
        "Total Cash": compact_profile_number(profile, "totalCash"),
        "Total Debt": compact_profile_number(profile, "totalDebt"),
        "Debt To Equity": profile_value(profile, "debtToEquity"),
        "Current Ratio": profile_value(profile, "currentRatio"),
    }


def analyst_ownership_signals(profile: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "Recommendation": profile_value(profile, "recommendationKey"),
        "Recommendation Mean": profile_value(profile, "recommendationMean"),
        "Target Mean Price": profile_value(profile, "targetMeanPrice"),
        "Target Median Price": profile_value(profile, "targetMedianPrice"),
        "Target High Price": profile_value(profile, "targetHighPrice"),
        "Target Low Price": profile_value(profile, "targetLowPrice"),
        "Analyst Opinion Count": profile_value(profile, "numberOfAnalystOpinions"),
        "Held By Insiders": profile_value(profile, "heldPercentInsiders"),
        "Held By Institutions": profile_value(profile, "heldPercentInstitutions"),
        "Dividend Yield": profile_value(profile, "dividendYield"),
        "Payout Ratio": profile_value(profile, "payoutRatio"),
    }


def management_snapshot(profile: Mapping[str, Any]) -> dict[str, Any]:
    officers = []
    raw_officers = profile.get("companyOfficers")
    if isinstance(raw_officers, list):
        for officer in raw_officers:
            if not isinstance(officer, dict):
                continue
            name = officer.get("name")
            title = officer.get("title")
            if missing_profile_value(name) or missing_profile_value(title):
                continue
            officers.append({"Name": str(name), "Title": str(title)})
            if len(officers) == 5:
                break

    return {
        "Executive Officers": officers or "N/A",
        "Governance Caveat": (
            "Officer roster comes from the profile provider when available. Board "
            "independence, ownership details by holder, compensation philosophy, and proxy "
            "filing context are not available in this dataset."
        ),
    }


def data_coverage(profile: Mapping[str, Any]) -> dict[str, str]:
    checks = {
        "Business Description": ("longBusinessSummary",),
        "Sector And Industry": ("sector", "industry"),
        "Management Roster": ("companyOfficers",),
        "Valuation Multiples": ("trailingPE", "forwardPE", "enterpriseToRevenue"),
        "Financial Quality": ("revenueGrowth", "profitMargins", "returnOnEquity"),
        "Analyst Expectations": ("targetMeanPrice", "recommendationKey"),
        "Ownership Signals": ("heldPercentInsiders", "heldPercentInstitutions"),
        "SEC Filings / MD&A": (),
        "Earnings Transcript / Guidance": (),
        "Peer Comps": (),
    }
    coverage = {}
    for label, keys in checks.items():
        if not keys:
            coverage[label] = "not supplied"
            continue
        coverage[label] = (
            "available"
            if any(not missing_profile_value(profile.get(key)) for key in keys)
            else "not supplied"
        )
    return coverage


def compact_profile_number(profile: Mapping[str, Any], key: str) -> Any:
    value = profile_value(profile, key, None)
    if isinstance(value, int | float):
        return compact_number(value)
    return "N/A" if value is None else value


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


def build_market_memo(
    client: MarketDataClient,
    ticker: str,
    *,
    text_generator: TextGenerator | None = None,
    api_key: str | None = None,
    text_model: str | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    report = build_stock_fax_data(client, ticker)
    generated = generate_market_memo_text(
        report,
        text_generator=text_generator,
        api_key=api_key,
        text_model=text_model,
        session=session,
    )
    return {
        "Ticker": report["Ticker"],
        "Market Memo": generated.text,
        "Report": report,
        "Text Provider": generated.provider,
        "Text Model": generated.model,
    }


def generate_stock_fax_text(
    report: dict[str, Any],
    *,
    text_generator: TextGenerator | None = None,
    api_key: str | None = None,
    text_model: str | None = None,
    session: requests.Session | None = None,
) -> GeneratedText:
    generator = text_generator or AnthropicTextClient(
        api_key=api_key,
        model=text_model,
        session=session,
    )
    return generator.generate_text(
        system=MARKET_TEXT_SYSTEM,
        prompt=(
            "Create a compact Stock Fax narrative in markdown for this ticker. Make it "
            "useful to an analyst who needs the business read and the tape read in one pass. "
            "Use exactly these sections: Business Read, Setup, Valuation/Fundamentals, "
            "Key Levels, Watch Next. Keep it under 280 words. Call out any material "
            "research gaps instead of filling them with generic commentary.\n\n"
            f"{json.dumps(text_report_payload(report), sort_keys=True, default=str)}"
        ),
        max_tokens=850,
        temperature=0.2,
    )


def generate_market_memo_text(
    report: dict[str, Any],
    *,
    text_generator: TextGenerator | None = None,
    api_key: str | None = None,
    text_model: str | None = None,
    session: requests.Session | None = None,
) -> GeneratedText:
    generator = text_generator or AnthropicTextClient(
        api_key=api_key,
        model=text_model,
        session=session,
    )
    return generator.generate_text(
        system=MARKET_TEXT_SYSTEM,
        prompt=market_memo_prompt(report),
        max_tokens=3200,
        temperature=0.2,
    )


def stream_market_memo_text(
    report: dict[str, Any],
    *,
    text_generator: TextGenerator | StreamingTextGenerator | None = None,
    api_key: str | None = None,
    text_model: str | None = None,
    session: requests.Session | None = None,
) -> Iterator[str]:
    generator = text_generator or AnthropicTextClient(
        api_key=api_key,
        model=text_model,
        session=session,
    )
    stream_text = getattr(generator, "stream_text", None)
    if callable(stream_text):
        yield from stream_text(
            system=MARKET_TEXT_SYSTEM,
            prompt=market_memo_prompt(report),
            max_tokens=3200,
            temperature=0.2,
        )
        return

    generated = generator.generate_text(
        system=MARKET_TEXT_SYSTEM,
        prompt=market_memo_prompt(report),
        max_tokens=3200,
        temperature=0.2,
    )
    yield generated.text


def market_memo_prompt(report: dict[str, Any]) -> str:
    return (
        f"Write a professional buyside-style equity analyst memo for {report['Ticker']} in "
        "markdown. Start with exactly "
        f"'### {report['Ticker']} Vision'. Target 950-1,300 words and use a sober, "
        "institutional voice. The memo should feel like a real investment note, not a "
        "technical indicator recap or newsletter teaser. Use only the provided structured "
        "data; do not invent news, earnings details, guidance, management commentary, "
        "products, macro facts, peer comps, or valuation history that is not present. "
        "When a normal analyst workstream is missing, say 'not supplied' and move it to "
        "the diligence agenda.\n\n"
        "Analytical standards:\n"
        "- Anchor every claim to a supplied field or clearly label it as an inference.\n"
        "- Discuss business quality, sector/industry context, equity performance, valuation, "
        "financial quality, management/execution, risk, and scenario levels.\n"
        "- Do not use generic catalysts like 'earnings could move the stock' unless the "
        "data contains a concrete earnings/guidance item.\n"
        "- Do not issue a personal buy/sell recommendation. A provisional stance is allowed "
        "only if framed as data-conditioned and uncertainty-aware.\n\n"
        "Use exactly these sections:\n"
        "1. **Executive Read** - 2-3 dense paragraphs with the provisional stance, what "
        "matters most, confidence level, and the strongest counterargument.\n"
        "2. **Company, Sector, And Business Model** - use the business description, sector, "
        "industry, geography, employee base, and what is missing from the company-level read.\n"
        "3. **Equity Performance And Positioning** - cover recent returns, distance from "
        "52-week high/low, trend, beta, liquidity/volume if available, and whether the stock "
        "looks crowded, extended, washed out, or balanced from the supplied data.\n"
        "4. **Valuation And Financial Quality** - discuss market cap, PE/EV/sales/book "
        "metrics, revenue growth, margins, ROE, leverage, cash/debt, free cash flow, and "
        "whether the valuation looks internally justified by quality signals. Do not compare "
        "to peers unless peer data is supplied.\n"
        "5. **Management And Execution** - identify supplied executives and assess execution "
        "only through supplied fundamentals, margins, growth, leverage, and business summary. "
        "If no management roster or MD&A is supplied, say so plainly.\n"
        "6. **Price Map** - a markdown table covering current price, 52-week high/low, VAH, "
        "VAL, POC, EMA21, EMA55, regression channel bounds, and any clear scenario trigger.\n"
        "7. **Catalysts, Risks, And Variant Perception** - separate data-supported items from "
        "research gaps. Include downside risks, upside evidence, and what the market may be "
        "under/overweighting based only on supplied fields.\n"
        "8. **Scenario Framework** - bullish, base, and bearish cases with specific levels "
        "or fundamental conditions that would confirm or invalidate each case.\n"
        "9. **Research Gaps / Next Diligence** - list the missing SEC 10-K/10-Q Business, "
        "Risk Factors, MD&A, financial statements, proxy/management compensation, latest "
        "earnings transcript, guidance, and peer-comparison work that should be checked before "
        "treating the memo as a full investment recommendation.\n\n"
        "Avoid investment advice promises. Make the memo useful to a serious analyst who wants "
        "a research starting point with a price map, not a short summary.\n\n"
        f"{json.dumps(text_report_payload(report), sort_keys=True, default=str)}"
    )


def generate_analysis_brief(
    summaries: list[dict[str, Any]],
    scanner: list[dict[str, Any]],
    *,
    text_generator: TextGenerator | None = None,
    api_key: str | None = None,
    text_model: str | None = None,
    session: requests.Session | None = None,
) -> GeneratedText:
    generator = text_generator or AnthropicTextClient(
        api_key=api_key,
        model=text_model,
        session=session,
    )
    payload = {"summaries": summaries, "scanner": scanner}
    return generator.generate_text(
        system=MARKET_TEXT_SYSTEM,
        prompt=(
            "Write a concise professional stock brief for this analysis response in markdown. "
            "If multiple tickers are present, lead with the scanner read, then call out the top "
            "one or two names and the main caveats. Include business/sector, valuation, quality, "
            "performance, and risk only where the supplied summaries support it. Keep it under "
            "260 words and avoid generic market commentary.\n\n"
            f"{json.dumps(payload, sort_keys=True, default=str)}"
        ),
        max_tokens=750,
        temperature=0.2,
    )


def text_report_payload(report: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in report.items() if key != "Export Rows"}


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
    style_axis(
        chart_ax,
        title=f"{ticker} moneyline",
        subtitle=f"expiry {expiry} | spot {current_price:.2f}",
        grid_axis="y",
    )
    strikes = [row["strike"] for row in rows]
    call_oi = [row["call_open_interest"] for row in rows]
    put_oi = [-row["put_open_interest"] for row in rows]
    chart_ax.bar(
        strikes,
        call_oi,
        width=1.2,
        color=GREEN,
        alpha=0.86,
        edgecolor=TEXT,
        linewidth=0.25,
        label="Call OI",
    )
    chart_ax.bar(
        strikes,
        put_oi,
        width=1.2,
        color=RED,
        alpha=0.82,
        edgecolor=TEXT,
        linewidth=0.25,
        label="Put OI",
    )
    chart_ax.axvline(current_price, color=AMBER, linewidth=2.4, label=f"Spot {current_price:.2f}")
    chart_ax.axhline(0, color=TEXT_STRONG, linewidth=1)
    chart_ax.set_xlabel("Strike")
    chart_ax.set_ylabel("Open interest mirror")
    format_absolute_y_axis(chart_ax)
    style_legend(chart_ax)

    table_ax.axis("off")
    table_ax.set_facecolor(AX_BG)
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
    table.scale(1, 1.35)
    for (row_index, _column_index), cell in table.get_celld().items():
        cell.set_edgecolor(CYAN if row_index == 0 else "#24444a")
        cell.set_linewidth(0.75 if row_index == 0 else 0.35)
        if row_index == 0:
            cell.set_facecolor(AMBER)
            cell.get_text().set_color(CHART_BG)
            cell.get_text().set_fontweight("bold")
        else:
            cell.set_facecolor(PANEL)
            cell.get_text().set_color(TEXT)
    table_ax.text(
        0,
        0.985,
        "STRIKE LADDER",
        transform=table_ax.transAxes,
        color=AMBER,
        fontsize=12,
        fontweight="bold",
        ha="left",
        va="top",
    )
    table_ax.text(
        0,
        0.93,
        "positive calls / negative puts",
        transform=table_ax.transAxes,
        color=MUTED,
        fontsize=9,
        fontweight="bold",
        ha="left",
        va="top",
    )
    fig.tight_layout(rect=(0, 0.06, 1, 0.96))
    add_terminal_footer(fig, left=f"{ticker} open interest mirror", right="moneyline")
    return image_from_figure(fig, f"{ticker.lower()}-moneyline.png")


def generate_pixel_image(
    prompt: str,
    *,
    api_key: str | None = None,
    image_model: str | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    clean_prompt = prompt.strip()
    if not clean_prompt:
        raise ValueError("Prompt is required")

    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key:
        raise MarketDataError("OPENAI_API_KEY is not configured for Pixel generation")
    model = image_model or os.getenv("OPENAI_IMAGE_MODEL") or DEFAULT_OPENAI_IMAGE_MODEL

    http = session or requests.Session()
    response = http.post(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
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
        "model": model,
        "prompt": clean_prompt,
        "styled_prompt": f"{PIXEL_STYLE} {clean_prompt}",
    }


def openai_error_text(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return friendly_openai_error(response.text or "OpenAI image request failed")
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return friendly_openai_error(str(error["message"]))
    return "OpenAI image request failed"


def friendly_openai_error(message: str) -> str:
    lowered = message.lower()
    if "billing hard limit" in lowered or "insufficient_quota" in lowered:
        return (
            "OpenAI billing limit reached for this project. Add or raise the project budget, "
            "wait a few minutes for it to propagate, or use a key from a project with available "
            "image-generation budget."
        )
    if "invalid api key" in lowered or "incorrect api key" in lowered:
        return "OpenAI API key was rejected. Check OPENAI_API_KEY in .env and the Modal secret."
    return message
