"""Chart-derived market context for written briefs.

Briefs used to see only profile fundamentals and the scanner pass, so the text
could never say where price sat against value, what the torque stage read, or how
options were positioned — even though the chart endpoints already compute all of
it. This module reuses those same builders and compresses each chart into the
handful of numbers a writer (or an agent) actually reasons over, so the model
gets the data behind the charts rather than a picture of them.

Every source is best effort: a ticker with no options chain, no SEC trend pack,
or too little history still returns a context, with the gap named in
``unavailable`` instead of silently dropped.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.chart_data import build_auction_chart_data, build_torque_chart_data
from app.market_data import HistoryResult, MarketDataClient
from app.tools import build_moneyline_data, clean_ticker

# Auction value levels need a full 21-bar window plus the live bar.
AUCTION_MINIMUM_BARS = 22

DEFAULT_CONTEXT_PERIOD = "6mo"
DEFAULT_CONTEXT_INTERVAL = "1d"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and value not in (float("inf"), float("-inf")) else None


def summarize_auction(history: HistoryResult, *, period: str) -> dict[str, Any]:
    """Value area, acceptance, and the traded range behind the auction chart."""
    dataset = build_auction_chart_data(history, period=period)
    meta = dataset.get("meta") or {}
    bars = dataset.get("series", {}).get("ohlcv") or []
    highs = [_number(bar.get("high")) for bar in bars]
    lows = [_number(bar.get("low")) for bar in bars]
    closes = [_number(bar.get("close")) for bar in bars]
    return {
        "period": period,
        "interval": dataset.get("interval"),
        "bars": len(bars),
        "vah": _number(meta.get("vah")),
        "val": _number(meta.get("val")),
        "poc": _number(meta.get("poc")),
        "location": meta.get("location"),
        "distance_to_poc": _number(meta.get("distance_to_poc")),
        "last_close": next((close for close in reversed(closes) if close is not None), None),
        "range_high": max((high for high in highs if high is not None), default=None),
        "range_low": min((low for low in lows if low is not None), default=None),
    }


def summarize_torque(
    *,
    history: HistoryResult | None,
    sec_trend: dict[str, Any] | None,
    profile: dict[str, Any] | None,
) -> dict[str, Any]:
    """Stage, score, and per-component detail behind the torque chart."""
    dataset = build_torque_chart_data(history=history, sec_trend=sec_trend, profile=profile)
    meta = dataset.get("meta") or {}
    price = dataset.get("series", {}).get("price") or {}

    def _last(key: str) -> float | None:
        points = price.get(key) or []
        return _number(points[-1].get("value")) if points else None

    return {
        "total_score": _number(meta.get("total_score")),
        "stage_label": meta.get("stage_label"),
        "stage_detail": meta.get("stage_detail"),
        "recommendation": meta.get("recommendation"),
        "target_zone": meta.get("target_zone"),
        "components": meta.get("components") or {},
        "fundamental_data_available": bool(meta.get("fundamental_data_available")),
        "close": _last("close"),
        "ema75": _last("ema75"),
        "sma50": _last("sma50"),
        "sma200": _last("sma200"),
    }


def summarize_moneyline(dataset: dict[str, Any]) -> dict[str, Any]:
    """Open-interest positioning behind the moneyline chart."""
    meta = dataset.get("meta") or {}
    strikes = dataset.get("series", {}).get("strikes") or []
    call_open_interest = sum(_number(row.get("call_open_interest")) or 0.0 for row in strikes)
    put_open_interest = sum(_number(row.get("put_open_interest")) or 0.0 for row in strikes)
    current_price = _number(meta.get("current_price"))
    peak = max(
        strikes,
        key=lambda row: (_number(row.get("call_open_interest")) or 0.0)
        + (_number(row.get("put_open_interest")) or 0.0),
        default=None,
    )
    nearest = (
        min(
            (row for row in strikes if _number(row.get("strike")) is not None),
            key=lambda row: abs((_number(row.get("strike")) or 0.0) - current_price),
            default=None,
        )
        if current_price is not None
        else None
    )
    return {
        "expiry": meta.get("expiry"),
        "current_price": current_price,
        "strikes_covered": len(strikes),
        "call_open_interest": call_open_interest,
        "put_open_interest": put_open_interest,
        "put_call_ratio": (put_open_interest / call_open_interest) if call_open_interest else None,
        "peak_open_interest_strike": _number(peak.get("strike")) if peak else None,
        "nearest_strike": _number(nearest.get("strike")) if nearest else None,
        "nearest_strike_net_open_interest": (
            _number(nearest.get("net_open_interest")) if nearest else None
        ),
    }


def build_market_context(
    client: MarketDataClient,
    ticker: str,
    *,
    sec_client: Any | None = None,
    history: HistoryResult | None = None,
    period: str = DEFAULT_CONTEXT_PERIOD,
    interval: str = DEFAULT_CONTEXT_INTERVAL,
    include_options: bool = True,
) -> dict[str, Any]:
    """Assemble one ticker's chart data as compact numbers, naming every gap.

    Pass ``history`` when the caller already fetched one for this ticker, so a
    memo or brief pays for a single price request instead of two.
    """
    symbol = clean_ticker(ticker)
    context: dict[str, Any] = {"ticker": symbol, "unavailable": []}

    def _unavailable(source: str, error: Exception | str) -> None:
        context["unavailable"].append({"source": source, "error": str(error)})

    if history is None:
        try:
            history = client.get_history(symbol, period=period, interval=interval)
        except Exception as exc:  # noqa: BLE001 - context is best effort; a gap is reported, not raised
            _unavailable("history", exc)
            return context

    context["provider"] = history.provider
    context["provider_note"] = history.note

    if len(history.data) < AUCTION_MINIMUM_BARS:
        _unavailable(
            "auction",
            f"{len(history.data)} bars returned; auction value levels need {AUCTION_MINIMUM_BARS}.",
        )
    else:
        try:
            context["auction"] = summarize_auction(history, period=period)
        except Exception as exc:  # noqa: BLE001 - see above
            _unavailable("auction", exc)

    def _load_profile() -> dict[str, Any]:
        try:
            return client.get_profile(symbol)
        except Exception:  # noqa: BLE001 - a missing profile is a gap, not a failure
            return {}

    def _load_sec_trend() -> dict[str, Any] | None:
        if sec_client is None:
            return None
        try:
            from app.sec_trend import build_sec_trend_pack

            return build_sec_trend_pack(sec_client, symbol, quarters=8)
        except Exception:  # noqa: BLE001 - SEC coverage is uneven by design
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        profile_future = executor.submit(_load_profile)
        sec_trend_future = executor.submit(_load_sec_trend)
        profile = profile_future.result()
        sec_trend = sec_trend_future.result()

    try:
        context["torque"] = summarize_torque(history=history, sec_trend=sec_trend, profile=profile)
    except Exception as exc:  # noqa: BLE001 - see above
        _unavailable("torque", exc)
    if sec_trend is None:
        _unavailable("sec_trend", "No SEC trend pack was available, so torque leans on technicals.")

    if include_options:
        try:
            chain = build_moneyline_data(symbol, market_client=client)
            context["options"] = summarize_moneyline(chain)
        except Exception as exc:  # noqa: BLE001 - options coverage varies by plan and ticker
            _unavailable("options", exc)

    return context


def collect_market_context(
    client: MarketDataClient,
    tickers: list[str],
    *,
    sec_client: Any | None = None,
    period: str = DEFAULT_CONTEXT_PERIOD,
    interval: str = DEFAULT_CONTEXT_INTERVAL,
    include_options: bool = True,
    max_tickers: int = 5,
) -> list[dict[str, Any]]:
    """Build context for several tickers concurrently, bounded so a batch stays cheap."""
    selected = tickers[:max_tickers]
    if not selected:
        return []
    slots: list[dict[str, Any] | None] = [None] * len(selected)
    with ThreadPoolExecutor(max_workers=min(4, len(selected))) as executor:
        futures = {
            executor.submit(
                build_market_context,
                client,
                ticker,
                sec_client=sec_client,
                period=period,
                interval=interval,
                include_options=include_options,
            ): index
            for index, ticker in enumerate(selected)
        }
        for future in futures:
            index = futures[future]
            try:
                slots[index] = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad ticker must not sink the batch
                slots[index] = {
                    "ticker": selected[index],
                    "unavailable": [{"source": "context", "error": str(exc)}],
                }
    return [slot for slot in slots if slot is not None]
