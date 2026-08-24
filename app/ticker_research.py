"""One bounded, chart-complete ticker research packet.

The terminal has several individual chart/data endpoints. This module collects
their shared inputs once and returns the exact data behind every single-ticker
chart in one payload. It is data-only, so callers can give it to an agent,
render a subset, or export it without waiting for an LLM or a PNG renderer.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Protocol

import pandas as pd

from app.chart_data import (
    build_auction_chart_data,
    build_flow_compass_chart_data,
    build_performance_chart_data,
    build_portfolio_chart_data,
    build_regression_chart_data,
    build_ridge_growth_chart_data,
    build_torque_chart_data,
    build_volatility_chart_data,
)
from app.market_context import summarize_moneyline
from app.market_data import HistoryResult, OptionChainResult, clean_ticker, option_chain_payload
from app.sec_trend import build_sec_trend_pack
from app.tools import moneyline_data_from_chain

RESEARCH_PERIODS = ("1mo", "3mo", "1y")
RESEARCH_INTERVAL = "1d"
SEASONALITY_PERIOD = "10y"
AUCTION_LEVEL_WINDOW = "21 completed daily sessions"
SOURCE_FANOUT_MAX_WORKERS = 3
BUNDLE_CONCURRENCY_PER_PROCESS = 2
BUNDLE_CONCURRENCY_PER_CLIENT = 1

# One Gunicorn worker must not turn repeated clicks into unbounded provider
# fan-out. Each admitted packet has a small source fan-out; MarketDataClient
# remains responsible for Massive retries, Retry-After, and its source caches.
_bundle_slots = threading.BoundedSemaphore(BUNDLE_CONCURRENCY_PER_PROCESS)
_client_slots_lock = threading.Lock()
_active_client_slots: dict[str, int] = {}


class TickerResearchClient(Protocol):
    """The bounded subset of MarketDataClient used by a research packet."""

    def get_history(self, ticker: str, *, period: str, interval: str) -> HistoryResult: ...

    def get_profile(self, ticker: str) -> dict[str, Any]: ...

    def get_option_chain(self, ticker: str) -> OptionChainResult: ...

    def get_snapshot(self, ticker: str) -> dict[str, Any]: ...


class TickerResearchBusyError(RuntimeError):
    """Raised when the per-process comprehensive-packet capacity is full."""


def try_acquire_ticker_research_client(client_key: str) -> bool:
    """Admit one in-flight packet per external client per process."""
    with _client_slots_lock:
        active = _active_client_slots.get(client_key, 0)
        if active >= BUNDLE_CONCURRENCY_PER_CLIENT:
            return False
        _active_client_slots[client_key] = active + 1
        return True


def release_ticker_research_client(client_key: str) -> None:
    """Release an external client's packet admission slot."""
    with _client_slots_lock:
        active = _active_client_slots.get(client_key, 0)
        if active <= 1:
            _active_client_slots.pop(client_key, None)
        else:
            _active_client_slots[client_key] = active - 1


def build_ticker_research_bundle(
    client: TickerResearchClient,
    ticker: str,
    *,
    sec_client: Any | None = None,
) -> dict[str, Any]:
    """Return all single-ticker chart data for 1M, 3M, and 1Y in one packet.

    Source failures are retained in ``meta.errors`` so missing options
    entitlements or SEC coverage cannot hide otherwise useful price analysis.
    Only malformed tickers and capacity saturation raise to the HTTP boundary.
    """
    symbol = clean_ticker(ticker)
    if not _bundle_slots.acquire(blocking=False):
        raise TickerResearchBusyError("Ticker research is busy; try again shortly.")

    try:
        return _build_ticker_research_bundle(client, symbol, sec_client=sec_client)
    finally:
        _bundle_slots.release()


def _build_ticker_research_bundle(
    client: TickerResearchClient,
    symbol: str,
    *,
    sec_client: Any | None,
) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    source_status: dict[str, str] = {}
    source_values = _load_sources(client, symbol, sec_client=sec_client, errors=errors)

    primary_history = _history_source(source_values, "history_10y", source_status, errors, symbol)
    benchmark_history = _history_source(
        source_values, "benchmark_history", source_status, errors, "SPY"
    )
    profile = _dict_source(source_values, "profile", source_status, errors, symbol)
    sec_trend = _sec_source(source_values, source_status, errors, symbol)
    option_chain = _option_source(source_values, source_status, errors, symbol)

    source_data: dict[str, Any] = {}
    if profile:
        source_data["profile"] = profile
    if sec_trend:
        source_data["sec_trend"] = sec_trend
    if option_chain is not None:
        source_data["options_chain"] = option_chain_payload(option_chain)

    # History obtains a live snapshot for its final bar when Massive can provide
    # one. Reading it after history normally hits the existing TTL cache rather
    # than adding another concurrent request.
    snapshot = _load_snapshot(client, symbol, source_status, errors)
    if snapshot is not None:
        source_data["snapshot"] = snapshot

    intervals: dict[str, dict[str, Any]] = {}
    if primary_history is not None:
        for period in RESEARCH_PERIODS:
            history = _slice_history(primary_history, period)
            benchmark = (
                _align_benchmark_history(benchmark_history, history)
                if benchmark_history is not None
                else None
            )
            if benchmark is not None and benchmark.data.empty:
                _record_error(
                    errors,
                    symbol,
                    f"{period}.portfolio_benchmark",
                    "No overlapping SPY benchmark history was available",
                )
                benchmark = None
            intervals[period] = {
                "period": period,
                "interval": history.interval,
                "provider": history.provider,
                "provider_note": history.note,
                "bars": len(history.data),
                "charts": _build_window_charts(
                    history,
                    period=period,
                    profile=profile,
                    sec_trend=sec_trend,
                    benchmark=benchmark,
                    errors=errors,
                ),
            }

    seasonality: dict[str, Any] | None = None
    if primary_history is not None:
        try:
            seasonality = build_performance_chart_data(
                primary_history, month=datetime.now(UTC).month
            )
        except Exception as exc:  # noqa: BLE001 - partial packet is intentional
            _record_error(errors, symbol, "seasonality", exc)

    options: dict[str, Any] | None = None
    if option_chain is not None:
        try:
            options = {"moneyline": moneyline_data_from_chain(option_chain)}
        except Exception as exc:  # noqa: BLE001 - preserve all other sources
            _record_error(errors, symbol, "moneyline", exc)

    providers = _providers(primary_history, benchmark_history, option_chain)
    meta = {
        "schema_version": "ticker-research/v1",
        "requested_periods": list(RESEARCH_PERIODS),
        "interval": RESEARCH_INTERVAL,
        "chart_sources": [
            "auction",
            "seasonality",
            "regression",
            "ridge_growth",
            "flow_compass",
            "torque",
            "portfolio",
            "volatility",
            "moneyline",
        ],
        "source_status": source_status,
        "error_count": len(errors),
        "errors": errors,
        "rate_limit": {
            "market_data": "Massive-first via MarketDataClient",
            "source_fanout_max_workers": SOURCE_FANOUT_MAX_WORKERS,
            "bundle_concurrency_per_process": BUNDLE_CONCURRENCY_PER_PROCESS,
            "bundle_concurrency_per_client": BUNDLE_CONCURRENCY_PER_CLIENT,
            "price_windows": "1M, 3M, and 1Y derive from one 10Y daily ticker pull",
            "benchmark_history": "SPY 1Y daily history powers portfolio benchmarks",
            "seasonality_history": "The shared 10Y ticker history powers seasonality",
        },
    }
    export = {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "ticker-research",
        "ticker": symbol,
        "tickers": [symbol],
        "provider": providers,
        "provider_note": "Comprehensive ticker research data",
        "image_files": [],
        "meta": meta,
    }
    return {
        "ticker": symbol,
        # Keep compact decision data first. The agent executor projects this
        # context, while direct endpoint callers retain every raw chart series.
        "agent_context": _agent_context(
            symbol,
            intervals=intervals,
            seasonality=seasonality,
            options=options,
            profile=profile,
            sec_trend=sec_trend,
            errors=errors,
        ),
        "intervals": intervals,
        "seasonality": seasonality,
        "options": options,
        "source_data": source_data,
        "provider": providers,
        "provider_note": "Comprehensive ticker research data",
        "meta": meta,
        "export": export,
    }


def _load_sources(
    client: TickerResearchClient,
    symbol: str,
    *,
    sec_client: Any | None,
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    loaders: dict[str, Callable[[], Any]] = {
        "history_10y": lambda: client.get_history(
            symbol, period=SEASONALITY_PERIOD, interval=RESEARCH_INTERVAL
        ),
        "benchmark_history": lambda: client.get_history(
            "SPY", period="1y", interval=RESEARCH_INTERVAL
        ),
        "profile": lambda: client.get_profile(symbol),
        "sec_trend": lambda: build_sec_trend_pack(sec_client, symbol, quarters=8),
        "options": lambda: client.get_option_chain(symbol),
    }
    values: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=min(SOURCE_FANOUT_MAX_WORKERS, len(loaders))) as executor:
        futures = {executor.submit(loader): source for source, loader in loaders.items()}
        for future in as_completed(futures):
            source = futures[future]
            try:
                values[source] = future.result()
            except Exception as exc:  # noqa: BLE001 - every source is optional
                _record_error(errors, symbol, source, exc)
    return values


def _history_source(
    values: dict[str, Any],
    source: str,
    status: dict[str, str],
    errors: list[dict[str, str]],
    symbol: str,
) -> HistoryResult | None:
    value = values.get(source)
    if isinstance(value, HistoryResult):
        status[source] = "available"
        return value
    status[source] = "unavailable"
    if source not in {error["source"] for error in errors}:
        _record_error(errors, symbol, source, "No history was returned")
    return None


def _dict_source(
    values: dict[str, Any],
    source: str,
    status: dict[str, str],
    errors: list[dict[str, str]],
    symbol: str,
) -> dict[str, Any]:
    value = values.get(source)
    if isinstance(value, dict) and value:
        status[source] = "available"
        return value
    status[source] = "unavailable"
    if source not in {error["source"] for error in errors}:
        _record_error(errors, symbol, source, "No profile fields were returned")
    return {}


def _sec_source(
    values: dict[str, Any],
    status: dict[str, str],
    errors: list[dict[str, str]],
    symbol: str,
) -> dict[str, Any] | None:
    value = values.get("sec_trend")
    if not isinstance(value, dict):
        status["sec_trend"] = "unavailable"
        if "sec_trend" not in {error["source"] for error in errors}:
            _record_error(errors, symbol, "sec_trend", "No SEC trend pack was returned")
        return None
    source_status = str(value.get("Status") or "unavailable").lower()
    status["sec_trend"] = source_status
    if source_status == "unavailable":
        detail = "; ".join(str(error) for error in value.get("Errors", []) if error)
        _record_error(errors, symbol, "sec_trend", detail or "SEC trend data is unavailable")
    return value


def _option_source(
    values: dict[str, Any],
    status: dict[str, str],
    errors: list[dict[str, str]],
    symbol: str,
) -> OptionChainResult | None:
    value = values.get("options")
    if isinstance(value, OptionChainResult):
        status["options"] = "available"
        return value
    status["options"] = "unavailable"
    if "options" not in {error["source"] for error in errors}:
        _record_error(errors, symbol, "options", "No options chain was returned")
    return None


def _load_snapshot(
    client: TickerResearchClient,
    symbol: str,
    status: dict[str, str],
    errors: list[dict[str, str]],
) -> dict[str, Any] | None:
    try:
        snapshot = client.get_snapshot(symbol)
    except Exception as exc:  # noqa: BLE001 - additive live-bar source
        status["snapshot"] = "unavailable"
        _record_error(errors, symbol, "snapshot", exc)
        return None
    if not isinstance(snapshot, dict):
        status["snapshot"] = "unavailable"
        _record_error(errors, symbol, "snapshot", "Snapshot response was not an object")
        return None
    status["snapshot"] = "available"
    return snapshot


def _slice_history(history: HistoryResult, period: str) -> HistoryResult:
    if history.data.empty:
        return history
    offset = {
        "1mo": pd.DateOffset(months=1),
        "3mo": pd.DateOffset(months=3),
        "1y": pd.DateOffset(years=1),
    }[period]
    start = history.data.index.max() - offset
    return replace(history, data=history.data.loc[history.data.index >= start].copy())


def _align_benchmark_history(benchmark: HistoryResult, history: HistoryResult) -> HistoryResult:
    """Restrict SPY to the exact ticker window before comparing returns."""
    if benchmark.data.empty or history.data.empty:
        return benchmark
    start = history.data.index.min()
    end = history.data.index.max()
    data = benchmark.data.loc[
        (benchmark.data.index >= start) & (benchmark.data.index <= end)
    ].copy()
    return replace(benchmark, data=data)


def _build_window_charts(
    history: HistoryResult,
    *,
    period: str,
    profile: dict[str, Any],
    sec_trend: dict[str, Any] | None,
    benchmark: HistoryResult | None,
    errors: list[dict[str, str]],
) -> dict[str, dict[str, Any]]:
    builders: dict[str, Callable[[], dict[str, Any]]] = {
        "auction": lambda: _build_bundle_auction_chart_data(history, period=period),
        "regression": lambda: build_regression_chart_data(history),
        "ridge_growth": lambda: build_ridge_growth_chart_data(history, period=period),
        "flow_compass": lambda: build_flow_compass_chart_data(history, period=period),
        "torque": lambda: build_torque_chart_data(
            history=history,
            sec_trend=sec_trend,
            profile=profile,
        ),
        "portfolio": lambda: build_portfolio_chart_data(
            [history], investment_per_stock=100.0, benchmark=benchmark
        ),
        "volatility": lambda: build_volatility_chart_data([history]),
    }
    charts: dict[str, dict[str, Any]] = {}
    for name, builder in builders.items():
        try:
            charts[name] = builder()
        except Exception as exc:  # noqa: BLE001 - one short window cannot sink the packet
            _record_error(errors, history.ticker, f"{period}.{name}", exc)
    return charts


def _build_bundle_auction_chart_data(history: HistoryResult, *, period: str) -> dict[str, Any]:
    """Keep the terminal's auction convention explicit across packet windows."""
    chart = build_auction_chart_data(history, period=period)
    meta = chart.get("meta")
    if isinstance(meta, dict):
        meta["level_window"] = AUCTION_LEVEL_WINDOW
    return chart


def _agent_context(
    symbol: str,
    *,
    intervals: dict[str, dict[str, Any]],
    seasonality: dict[str, Any] | None,
    options: dict[str, Any] | None,
    profile: dict[str, Any],
    sec_trend: dict[str, Any] | None,
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "ticker": symbol,
        "intervals": {
            period: _window_context(packet.get("charts") or {})
            for period, packet in intervals.items()
        },
        "seasonality": _pick((seasonality or {}).get("meta"), "selected_month", "mean_5y"),
        "options": _options_context((options or {}).get("moneyline")),
        "profile": _pick(
            profile,
            "symbol",
            "longName",
            "shortName",
            "sector",
            "industry",
            "marketCap",
            "trailingPE",
            "forwardPE",
            "priceToSalesTrailing12Months",
        ),
        "sec_trend": _pick(
            sec_trend or {},
            "Status",
            "Company Name",
            "Revenue Acceleration",
            "Margin Trajectory",
            "Operating Leverage",
            "Errors",
        ),
        "unavailable": errors,
    }


def _window_context(charts: dict[str, Any]) -> dict[str, Any]:
    auction = _dataset_meta(charts.get("auction"))
    regression = _dataset_meta(charts.get("regression"))
    ridge = _dataset_meta(charts.get("ridge_growth"))
    flow = _dataset_meta(charts.get("flow_compass"))
    torque = _dataset_meta(charts.get("torque"))
    portfolio = _dataset_meta(charts.get("portfolio"))
    volatility = _mapping(charts.get("volatility"))
    return {
        "auction": _pick(
            auction,
            "vah",
            "val",
            "poc",
            "location",
            "distance_to_poc",
            "level_window",
        ),
        "regression": _pick(regression, "slope_per_day", "residual_std", "intercept"),
        "ridge_growth": _pick(
            ridge,
            "state",
            "recommendation",
            "total_return",
            "max_drawdown",
            "trend_confirmed",
            "latest_close",
            "flow_compass",
            "auction",
        ),
        "flow_compass": _pick(
            flow,
            "state",
            "score",
            "signal",
            "fresh_long",
            "fresh_short",
        ),
        "torque": _pick(
            torque,
            "total_score",
            "stage_label",
            "stage_detail",
            "recommendation",
            "target_zone",
            "fundamental_data_available",
        ),
        "portfolio": _pick(
            portfolio,
            "portfolio_final",
            "total_return",
            "max_drawdown",
            "annualized_volatility",
        ),
        "volatility": (volatility.get("rows") or [None])[0],
    }


def _options_context(dataset: Any) -> dict[str, Any]:
    if not isinstance(dataset, dict):
        return {}
    return _pick(
        summarize_moneyline(dataset),
        "expiry",
        "current_price",
        "strikes_covered",
        "call_open_interest",
        "put_open_interest",
        "put_call_ratio",
    )


def _dataset_meta(dataset: Any) -> dict[str, Any]:
    return _mapping(dataset.get("meta")) if isinstance(dataset, dict) else {}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _pick(source: Any, *keys: str) -> dict[str, Any]:
    return {key: source[key] for key in keys if isinstance(source, dict) and key in source}


def _providers(
    primary_history: HistoryResult | None,
    benchmark_history: HistoryResult | None,
    option_chain: OptionChainResult | None,
) -> str:
    providers = {
        source.provider
        for source in (primary_history, benchmark_history, option_chain)
        if source is not None and source.provider
    }
    return "+".join(sorted(providers)) or "unavailable"


def _record_error(
    errors: list[dict[str, str]],
    ticker: str,
    source: str,
    error: Exception | str,
) -> None:
    errors.append({"ticker": ticker, "source": source, "error": str(error)})
