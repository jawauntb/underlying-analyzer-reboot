"""Orchestration: one ticker in, one ``PrismPacket`` out.

Every section is built inside its own guard. A dead FRED key, an option
entitlement the account does not have, an SEC document that will not parse, or a
quant module that is not on disk yet each cost exactly one section: the section
becomes ``None``, its ``<section>_error`` sibling carries the reason, and
``meta.errors`` records it. The packet shape never changes, so the API proxy, the
dashboard and the agent tools can index the same keys on every build.

The W1 (data / macro / seasonality / relational) and W2 (factors / regimes /
entropy / spectral / eigen / scenarios) modules are imported lazily inside those
guards for the same reason: a partial checkout degrades section by section
instead of failing to import.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pandas as pd

from app.prism.contract import (
    ENGINE_VERSION,
    Provenance,
    empty_packet,
    record_error,
    record_source,
    record_timing,
    record_unavailable,
    set_section,
)

DEFAULT_YEARS = 10
DEFAULT_MAX_WORKERS = 4
LEVELS_PERIOD = "1y"
REFERENCE_SYMBOL = "SPY"
#: Monthly out-of-sample holdout for the eigen load-bearing test (of ~112 rows).
EIGEN_HOLDOUT_MONTHS = 36

class PrismEngineError(RuntimeError):
    """Raised only when the packet cannot be started at all (e.g. no ticker)."""


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@contextmanager
def _timed(packet: dict[str, Any], name: str) -> Any:
    """Time one build step into ``meta.timings_ms``."""
    started = time.perf_counter()
    try:
        yield
    finally:
        record_timing(packet, name, (time.perf_counter() - started) * 1000.0)


def _guard(packet: dict[str, Any], section: str, builder: Callable[[], Any]) -> Any:
    """Run one section builder; a failure sets the section to ``None`` with a reason."""
    with _timed(packet, section):
        try:
            value = builder()
        except Exception as exc:  # noqa: BLE001 - one section must never sink the build
            set_section(packet, section, None, error=f"{type(exc).__name__}: {exc}")
            return None
        if value is None:
            set_section(packet, section, None, error="builder returned nothing")
            return None
        set_section(packet, section, value)
        packet["meta"].setdefault("source_status", {})[section] = "available"
        _record_section_errors(packet, section, value)
        return value


def _record_section_errors(packet: dict[str, Any], section: str, value: Any) -> None:
    """Lift a section's own ``errors`` list into ``meta.unavailable``.

    A section can be present and still be missing a source — SPY has no 10-K and
    Massive has no income statement for an ETF. Those reasons live inside the
    section, where a reader who only checks ``meta`` would never see them.
    """
    if not isinstance(value, Mapping):
        return
    for reason in value.get("errors") or []:
        if isinstance(reason, str) and reason.strip():
            record_unavailable(packet, section, reason)
        elif isinstance(reason, Mapping):
            record_unavailable(
                packet,
                str(reason.get("source") or section),
                str(reason.get("error") or reason.get("reason") or reason),
            )


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------


#: The sector an ETF stands for, used to infer a sector from an SIC industry
#: string. Massive's reference endpoint carries the SIC description but no GICS
#: sector, and a dashboard with an empty sector chip is worse than a labelled
#: inference.
ETF_SECTOR_LABELS: dict[str, str] = {
    "XLK": "Technology",
    "XLY": "Consumer Discretionary",
    "XLV": "Health Care",
    "XLU": "Utilities",
    "XLB": "Materials",
    "XLC": "Communication Services",
    "XLF": "Financials",
    "XLP": "Consumer Staples",
    "XLE": "Energy",
    "XLI": "Industrials",
    "XLRE": "Real Estate",
    "SOXX": "Technology",
    "XBI": "Health Care",
    "URA": "Energy",
    "REMX": "Materials",
    "CPER": "Materials",
    "GLD": "Materials",
    "USO": "Energy",
    "DBA": "Consumer Staples",
}


def infer_sector(industry: Any, description: Any) -> tuple[str | None, bool]:
    """``(sector, was_inferred)`` from an SIC industry string, or ``(None, False)``."""
    from app.prism.universe import industry_etfs, sector_etf

    direct = sector_etf(industry)
    if direct and direct in ETF_SECTOR_LABELS:
        return ETF_SECTOR_LABELS[direct], True
    for etf in industry_etfs(industry, description):
        if etf in ETF_SECTOR_LABELS:
            return ETF_SECTOR_LABELS[etf], True
    return None, False


def build_profile(client: Any, ticker: str) -> dict[str, Any]:
    """Normalise the provider profile onto the packet's ``profile`` shape."""
    from app.prism.universe import related_etfs

    raw = client.get_profile(ticker) or {}
    massive = raw.get("massive") if isinstance(raw.get("massive"), Mapping) else {}
    industry = raw.get("industry") or raw.get("sector")
    sector = raw.get("sector")
    sector_inferred = False
    if not sector:
        sector, sector_inferred = infer_sector(industry, raw.get("longBusinessSummary"))
    return {
        "name": raw.get("longName") or raw.get("shortName") or ticker,
        "sector": sector,
        "sector_inferred": sector_inferred,
        "industry": industry,
        "market_cap": _finite(raw.get("marketCap")),
        "description": raw.get("longBusinessSummary"),
        "listed_since": (massive or {}).get("list_date") or raw.get("list_date"),
        "primary_exchange": (massive or {}).get("primary_exchange") or raw.get("exchange"),
        "currency": raw.get("currency"),
        "country": raw.get("country"),
        "employees": raw.get("fullTimeEmployees"),
        "website": raw.get("website"),
        "cik": raw.get("cik"),
        "related_etfs": related_etfs(
            {
                "sector": sector,
                "industry": industry,
                "longBusinessSummary": raw.get("longBusinessSummary"),
            }
        ),
        "provider": "massive",
        "fetched_at": datetime.now(UTC).isoformat(),
    }


# --------------------------------------------------------------------------
# Derived helpers shared by several sections
# --------------------------------------------------------------------------


def monthly_returns(series: pd.Series) -> pd.Series:
    """Calendar-month simple returns of a daily close series."""
    prices = pd.to_numeric(pd.Series(series), errors="coerce").dropna()
    prices = prices[prices > 0]
    if prices.shape[0] < 2:
        return pd.Series(dtype="float64")
    if not isinstance(prices.index, pd.DatetimeIndex):
        converted = pd.to_datetime(prices.index, errors="coerce")
        prices = prices[converted.notna()]
        prices.index = converted[converted.notna()]
    return prices.resample("ME").last().pct_change().dropna()


def build_signal_frame(
    ticker: str,
    series_map: Mapping[str, pd.Series],
    *,
    impact_symbols: Sequence[str] = (),
    limit: int = 10,
) -> tuple[pd.DataFrame, pd.Series]:
    """Monthly candidate signals and the ticker's monthly return, aligned.

    Benchmarks come first (the ones the relational section says carry the most of
    this ticker's variance), then three price-derived features that are not just
    another index return: trailing realized volatility, six-month momentum and
    distance from the 200-day average.
    """
    close = series_map.get(ticker)
    if close is None or len(close) < 60:
        return pd.DataFrame(), pd.Series(dtype="float64")
    target = monthly_returns(close)
    if target.empty:
        return pd.DataFrame(), pd.Series(dtype="float64")

    columns: dict[str, pd.Series] = {}
    ordered = [symbol for symbol in impact_symbols if symbol != ticker]
    for symbol in series_map:
        if symbol != ticker and symbol not in ordered:
            ordered.append(symbol)
    for symbol in ordered[: max(1, int(limit))]:
        monthly = monthly_returns(series_map[symbol])
        if monthly.shape[0] >= 24:
            columns[symbol] = monthly

    prices = pd.to_numeric(pd.Series(close), errors="coerce").dropna()
    if not isinstance(prices.index, pd.DatetimeIndex):
        converted = pd.to_datetime(prices.index, errors="coerce")
        prices = prices[converted.notna()]
        prices.index = converted[converted.notna()]
    daily = prices.pct_change().dropna()
    columns["realized_vol_21d"] = (
        daily.rolling(21).std(ddof=1).mul(math.sqrt(252.0)).resample("ME").last().dropna()
    )
    columns["momentum_126d"] = (prices / prices.shift(126) - 1.0).resample("ME").last().dropna()
    moving_average = prices.rolling(200).mean()
    columns["dist_from_ma200"] = (
        (prices / moving_average - 1.0).resample("ME").last().dropna()
    )

    frame = pd.DataFrame(columns).dropna(how="all")
    if frame.empty:
        return pd.DataFrame(), target
    aligned = frame.reindex(target.index).dropna(how="all")
    return aligned, target.reindex(aligned.index).dropna()


def signal_prediction_history(
    signals: pd.DataFrame, target: pd.Series
) -> tuple[pd.DataFrame, pd.Series]:
    """What each signal *would have* forecast, using only prior months.

    For month ``t`` a signal's prediction is the mean next-month return of every
    earlier month whose signal value shared the current sign, counted only once
    that month's own forward return had already settled. That makes the
    leave-one-out weighting in :func:`app.prism.eigen.load_bearing_test` a real
    intervention on out-of-sample skill rather than a rescaling of a prior.
    """
    if signals.empty or target.empty:
        return pd.DataFrame(), pd.Series(dtype="float64")
    frame = signals.join(target.rename("__target__"), how="inner").dropna(how="all")
    if frame.shape[0] < 36:
        return pd.DataFrame(), pd.Series(dtype="float64")

    realized = frame["__target__"].shift(-1)
    predictions: dict[str, pd.Series] = {}
    index = frame.index
    for column in signals.columns:
        values = frame[column].to_numpy(dtype=np.float64)
        forward = realized.to_numpy(dtype=np.float64)
        output = np.full(values.shape[0], np.nan, dtype=np.float64)
        buckets: dict[bool, list[float]] = {True: [], False: []}
        for position in range(values.shape[0]):
            settled = position - 1
            if settled >= 0 and np.isfinite(values[settled]) and np.isfinite(forward[settled]):
                buckets[bool(values[settled] > 0)].append(float(forward[settled]))
            if not np.isfinite(values[position]):
                continue
            history = buckets[bool(values[position] > 0)]
            if len(history) >= 3:
                output[position] = float(np.mean(history))
        series = pd.Series(output, index=index, name=str(column)).dropna()
        if series.shape[0] >= 24:
            predictions[str(column)] = series
    if not predictions:
        return pd.DataFrame(), pd.Series(dtype="float64")
    return pd.DataFrame(predictions), realized.dropna()


def build_recent(
    ticker: str,
    series_map: Mapping[str, pd.Series],
    *,
    sector_symbol: str | None,
    entropy_section: Mapping[str, Any] | None,
    regimes_section: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The last 20 and 60 sessions, in the packet's ``recent`` shape."""
    close = series_map.get(ticker)
    if close is None or len(close) < 25:
        raise ValueError("not enough price history for a recent-window read")
    prices = pd.to_numeric(pd.Series(close), errors="coerce").dropna()
    reference = series_map.get(REFERENCE_SYMBOL)
    sector = series_map.get(sector_symbol) if sector_symbol else None
    regime_label = ((regimes_section or {}).get("current") or {}).get("label")

    def window(days: int) -> dict[str, Any]:
        tail = prices.iloc[-(days + 1) :]
        if tail.shape[0] < 3:
            raise ValueError(f"not enough history for a {days}-day window")
        change = float(tail.iloc[-1] / tail.iloc[0] - 1.0)
        returns = tail.pct_change().dropna()
        volatility = (
            float(returns.std(ddof=1) * math.sqrt(252.0)) if returns.shape[0] > 2 else None
        )

        def relative(other: pd.Series | None) -> float | None:
            if other is None or len(other) < days + 1:
                return None
            other_tail = pd.to_numeric(pd.Series(other), errors="coerce").dropna()
            other_tail = other_tail.iloc[-(days + 1) :]
            if other_tail.shape[0] < 3 or float(other_tail.iloc[0]) == 0:
                return None
            return change - float(other_tail.iloc[-1] / other_tail.iloc[0] - 1.0)

        entropy_value: float | None = None
        windows = (entropy_section or {}).get("windows")
        if isinstance(windows, Mapping):
            key = "1m" if days <= 25 else "3m"
            block = windows.get(key)
            if isinstance(block, Mapping):
                entropy_value = _finite(block.get("H"))
        notable: list[str] = []
        if volatility is not None and volatility > 0.6:
            notable.append(f"annualised volatility ran at {volatility:.0%}")
        spy_relative = relative(reference)
        if spy_relative is not None and abs(spy_relative) > 0.05:
            notable.append(
                f"{'out' if spy_relative > 0 else 'under'}performed SPY by "
                f"{abs(spy_relative):.1%}"
            )
        return {
            "return": change,
            "vs_spy": spy_relative,
            "vs_sector": relative(sector),
            "sector_symbol": sector_symbol,
            "volatility": volatility,
            "entropy": entropy_value,
            "regime": regime_label,
            "notable": "; ".join(notable) or None,
            "n_days": int(tail.shape[0] - 1),
        }

    return {"last_20d": window(20), "last_60d": window(60)}


# --------------------------------------------------------------------------
# The build
# --------------------------------------------------------------------------


def build_prism_packet(
    client: Any,
    ticker: str,
    *,
    sec_client: Any | None = None,
    exa_client: Any | None = None,
    text_generator: Any | None = None,
    as_of: date | str | None = None,
    include_memo: bool = True,
    force: bool = False,
    cache: Any | None = None,
    store: Any | None = None,
    fred_client: Any | None = None,
    years: int = DEFAULT_YEARS,
    api_key: str | None = None,
    text_model: str | None = None,
    persist: bool = True,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict[str, Any]:
    """Build one full packet for ``ticker``.

    ``force`` bypasses the stored packet for today and rebuilds from the sources.
    ``include_memo=False`` returns everything except the narrative, which is the
    cheap path for a dashboard refresh.
    """
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        raise PrismEngineError("ticker is required")

    started = time.perf_counter()
    resolved_as_of = _resolve_as_of(as_of)

    if not force and persist:
        existing = get_prism_packet(symbol, resolved_as_of, store=store)
        if existing is not None:
            existing.setdefault("meta", {}).setdefault("cache", {})["packet"] = "hit"
            return existing

    packet = empty_packet(symbol, as_of=resolved_as_of)
    packet["engine_version"] = ENGINE_VERSION
    packet["meta"]["cache"] = {"packet": "miss"}

    prism_cache = cache
    if prism_cache is None:
        try:
            from app.prism.cache import PrismCache

            prism_cache = PrismCache.from_env()
        except Exception as exc:  # noqa: BLE001
            record_error(packet, "cache", f"cache unavailable: {exc}")
            prism_cache = None

    # Resolve the narrative model once. Callers may hand in a generator (tests,
    # the CLI) or just an API key (the HTTP layer, which keeps app.config's
    # TEXT_GENERATOR at None). Without this, the filing summaries and the
    # cross-filing synthesis silently fell back to raw excerpts on every HTTP
    # build while the memo — which resolves its own key — used the model.
    if text_generator is None and api_key:
        try:
            from app.anthropic import AnthropicTextClient

            text_generator = AnthropicTextClient(api_key=api_key, model=text_model)
        except Exception as exc:  # noqa: BLE001
            record_unavailable(packet, "anthropic", f"text generator unavailable: {exc}")
    if text_generator is None:
        record_unavailable(
            packet,
            "anthropic",
            "no text generator or API key; narrative sections use the deterministic template",
        )

    profile = _guard(packet, "profile", lambda: build_profile(client, symbol)) or {}

    # ---------------------------------------------------------------- fan out
    fetched = _fan_out(
        packet,
        client,
        symbol,
        profile=profile,
        sec_client=sec_client,
        exa_client=exa_client,
        text_generator=text_generator,
        cache=prism_cache,
        fred_client=fred_client,
        as_of=resolved_as_of,
        years=years,
        max_workers=max_workers,
    )

    series_map: dict[str, pd.Series] = dict(fetched.get("series") or {})
    close = series_map.get(symbol)
    current_price = float(close.iloc[-1]) if close is not None and len(close) else None

    # Fundamentals runs after the fan-out rather than inside it because every
    # price multiple has to be struck against the *same* price the rest of the
    # packet quotes. Massive's ratios snapshot carries its own price (220.78 for
    # NVDA on 2026-09-01 against a 217.48 last close), so a P/E derived from it
    # would silently disagree with the entry band, the levels and the memo by
    # more than a percent. The extra 1-2s is serial, not parallel, but it buys
    # one price for the whole packet.
    fetched["fundamentals"] = _guard(
        packet,
        "fundamentals",
        lambda: _build_fundamentals(
            client, symbol, sec_client, profile, current_price
        ),
    )
    fundamentals_section = packet.get("fundamentals")
    if isinstance(fundamentals_section, Mapping):
        for row in fundamentals_section.get("sources") or []:
            if isinstance(row, Mapping):
                record_source(packet, Provenance(**dict(row)))

    # ------------------------------------------------------------ W1 sections
    benchmarks = {
        name: series for name, series in series_map.items() if name != symbol
    }
    _guard(
        packet,
        "seasonality",
        lambda: _require(close, "no price history")
        and _build_seasonality(symbol, close, benchmarks, profile, resolved_as_of),
    )
    _guard(
        packet,
        "relational",
        lambda: _require(close, "no price history") and _build_relational(
            symbol, series_map, resolved_as_of
        ),
    )

    # ------------------------------------------------------------ W2 sections
    _guard(
        packet,
        "factors",
        lambda: _require(close, "no price history")
        and _build_factors(close, series_map, packet, prism_cache),
    )
    regime_labels = _build_regimes_section(packet, symbol, series_map)
    _guard(
        packet,
        "entropy",
        lambda: _require(close, "no price history") and _build_entropy(close),
    )
    _guard(
        packet,
        "spectral",
        lambda: _require(close, "no price history") and _build_spectral(close),
    )

    # ------------------------------------------------------------ W3 sections
    for name in ("filings", "news"):
        payload = fetched.get(name)
        error = fetched.get(f"{name}_error")
        if payload is None:
            set_section(packet, name, None, error=str(error or "not built"))
        else:
            set_section(packet, name, payload)
            packet["meta"].setdefault("source_status", {})[name] = "available"
            _record_section_errors(packet, name, payload)

    _guard(
        packet,
        "volatility",
        lambda: _require(close, "no price history")
        and _build_volatility(close, client, symbol, regime_labels, resolved_as_of),
    )
    history = fetched.get("history")
    _guard(
        packet,
        "levels",
        lambda: _require(history, str(fetched.get("history_error") or "no OHLCV history"))
        and _build_levels(history, fetched.get("sec_trend"), profile, current_price),
    )

    # -------------------------------------------------------------- scenarios
    _guard(
        packet,
        "scenarios",
        lambda: _build_scenarios(
            packet, close, current_price, regime_labels, symbol, series_map
        ),
    )
    _guard(
        packet,
        "eigen",
        lambda: _build_eigen(packet, symbol, series_map, regime_labels),
    )
    _guard(
        packet,
        "recent",
        lambda: build_recent(
            symbol,
            series_map,
            sector_symbol=next(iter(profile.get("related_etfs") or []), None),
            entropy_section=packet.get("entropy"),
            regimes_section=packet.get("regimes"),
        ),
    )

    # ------------------------------------------------------------------- memo
    if include_memo:
        _guard(
            packet,
            "memo",
            lambda: _build_memo(packet, text_generator, api_key, text_model),
        )
    else:
        set_section(packet, "memo", None, error="include_memo=False")

    packet["meta"]["timings_ms"]["total"] = round((time.perf_counter() - started) * 1000.0, 3)
    if prism_cache is not None:
        packet["meta"]["cache"]["series"] = (
            "hit" if getattr(prism_cache, "hits", 0) else "miss"
        )
        packet["meta"]["cache"]["hits"] = int(getattr(prism_cache, "hits", 0))
        packet["meta"]["cache"]["misses"] = int(getattr(prism_cache, "misses", 0))

    if persist:
        try:
            from app.prism.store import save_packet

            stored = save_packet(packet, store=store)
            packet["meta"]["stored"] = stored
            # A tier that refused the write (typically Supabase before the
            # migration is applied) is a degraded source, not a lost packet: the
            # local JSON tier still holds it. Say so where the reviewers look.
            for reason in (stored or {}).get("errors") or []:
                record_unavailable(packet, "store", str(reason))
        except Exception as exc:  # noqa: BLE001 - a failed write must not lose the packet
            record_error(packet, "store", f"could not persist packet: {exc}")
    return packet


def _require(value: Any, reason: str) -> bool:
    """Raise ``ValueError(reason)`` when a prerequisite is missing."""
    if value is None or (hasattr(value, "__len__") and len(value) == 0):
        raise ValueError(reason)
    return True


def _resolve_as_of(as_of: date | str | None) -> str:
    """Normalise ``as_of`` to ``YYYY-MM-DD``, refusing anything else.

    ``as_of`` becomes the stored packet's filename and reaches the export's
    ``Content-Disposition`` header, so an unparseable value must raise rather
    than fall back to ``str(as_of)[:10]``.
    """
    if as_of is None or (isinstance(as_of, str) and not as_of.strip()):
        return datetime.now(UTC).date().isoformat()
    if isinstance(as_of, datetime):
        return as_of.date().isoformat()
    if isinstance(as_of, date):
        return as_of.isoformat()
    try:
        from app.prism.data import resolve_as_of

        return resolve_as_of(as_of).isoformat()
    except Exception as exc:  # noqa: BLE001
        raise PrismEngineError(f"as_of must be an ISO date (YYYY-MM-DD): {as_of!r}") from exc


# --------------------------------------------------------------------------
# Fan-out: every independent network fetch, four at a time
# --------------------------------------------------------------------------


def _fan_out(
    packet: dict[str, Any],
    client: Any,
    symbol: str,
    *,
    profile: Mapping[str, Any],
    sec_client: Any | None,
    exa_client: Any | None,
    text_generator: Any | None,
    cache: Any | None,
    fred_client: Any | None,
    as_of: str,
    years: int,
    max_workers: int,
) -> dict[str, Any]:
    """Run the seven independent source fetches concurrently."""
    results: dict[str, Any] = {}

    def _universe() -> Any:
        from app.prism.data import load_universe
        from app.prism.universe import resolve_universe, universe_symbols

        symbols = universe_symbols(symbol, profile=dict(profile))
        data = load_universe(client, symbols, years=years, as_of=as_of, cache=cache)
        entries = resolve_universe(symbol, profile=dict(profile))
        labels = {
            str(entry.get("symbol")): (
                str(entry.get("label") or entry.get("symbol")),
                str(entry.get("role") or "index"),
            )
            for entry in entries
        }
        return data, data.universe_entries(labels), entries

    def _macro() -> Any:
        from app.prism.macro import build_macro_section, fred_client_from_env

        fred = fred_client or fred_client_from_env()
        return build_macro_section(
            fred, market_client=client, as_of=as_of, cache=cache, years=years
        )

    def _filings() -> Any:
        from app.prism.filings import build_filings

        return build_filings(sec_client, symbol, text_generator=text_generator)

    def _news() -> Any:
        from app.prism.news import build_news

        return build_news(
            exa_client,
            symbol,
            company_name=str(profile.get("name") or "") or None,
            sector=str(profile.get("sector") or "") or None,
            industry=str(profile.get("industry") or "") or None,
            market_client=client,
        )

    def _history() -> Any:
        return client.get_history(symbol, period=LEVELS_PERIOD, interval="1d")

    def _sec_trend() -> Any:
        if sec_client is None:
            return None
        from app.sec_trend import build_sec_trend_pack

        return build_sec_trend_pack(sec_client, symbol)

    tasks: dict[str, Callable[[], Any]] = {
        "universe": _universe,
        "macro": _macro,
        "filings": _filings,
        "news": _news,
        "history": _history,
        "sec_trend": _sec_trend,
    }
    with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as pool:
        futures = {
            name: pool.submit(_timed_call(packet, name, task))
            for name, task in tasks.items()
        }
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except Exception as exc:  # noqa: BLE001
                results[name] = None
                results[f"{name}_error"] = f"{type(exc).__name__}: {exc}"
                record_error(packet, name, f"{type(exc).__name__}: {exc}")

    universe = results.pop("universe", None)
    if universe is not None:
        data, entries, resolved = universe
        results["series"] = dict(data.series)
        packet["universe"] = [dict(entry) for entry in entries]
        for entry in resolved:
            symbol_name = str(entry.get("symbol"))
            if entry.get("role") in {"rates", "vol", "macro"} and symbol_name not in data.series:
                packet["universe"].append(dict(entry))
        for row in data.provenance():
            record_source(packet, row)
        packet["meta"]["source_status"]["universe"] = (
            "available" if data.series else "unavailable"
        )
        for failed, error in data.errors.items():
            record_unavailable(packet, f"series:{failed}", error)
    else:
        results["series"] = {}
        packet["meta"]["source_status"]["universe"] = "unavailable"

    macro = results.get("macro")
    if macro is None:
        set_section(
            packet, "macro", None, error=str(results.get("macro_error") or "macro build failed")
        )
    else:
        set_section(packet, "macro", macro)
        packet["meta"]["source_status"]["macro"] = "available"
        for row in macro.get("unavailable") or []:
            if isinstance(row, Mapping):
                record_unavailable(packet, str(row.get("source")), str(row.get("reason")))

    for name in ("filings", "news"):
        payload = results.get(name)
        if isinstance(payload, Mapping):
            for row in payload.get("sources") or []:
                if isinstance(row, Mapping):
                    record_source(packet, Provenance(**dict(row)))
    return results


def _timed_call(
    packet: dict[str, Any], name: str, task: Callable[[], Any]
) -> Callable[[], Any]:
    def runner() -> Any:
        with _timed(packet, f"fetch.{name}"):
            return task()

    return runner


# --------------------------------------------------------------------------
# Section builders (thin wrappers so the guards stay readable)
# --------------------------------------------------------------------------


def _build_fundamentals(
    client: Any,
    symbol: str,
    sec_client: Any | None,
    profile: Mapping[str, Any],
    current_price: float | None,
) -> Any:
    from app.prism.fundamentals import build_fundamentals

    return build_fundamentals(
        client,
        symbol,
        sec_client=sec_client,
        current_price=current_price,
        market_cap=_finite(profile.get("market_cap")),
    )


def _build_seasonality(
    symbol: str,
    close: pd.Series,
    benchmarks: Mapping[str, pd.Series],
    profile: Mapping[str, Any],
    as_of: str,
) -> dict[str, Any]:
    from app.prism.seasonality import build_seasonality_section
    from app.prism.universe import CORE_BENCHMARKS

    wanted = list(dict.fromkeys([*CORE_BENCHMARKS, *(profile.get("related_etfs") or [])]))
    selected = {name: benchmarks[name] for name in wanted if name in benchmarks}
    if not selected:
        selected = dict(list(benchmarks.items())[:6])
    return build_seasonality_section(symbol, close, selected, as_of=as_of)


def _build_relational(symbol: str, series_map: Mapping[str, pd.Series], as_of: str) -> Any:
    from app.prism.relational import build_relational_section

    return build_relational_section(symbol, dict(series_map), as_of=as_of)


def _build_factors(
    close: pd.Series,
    series_map: Mapping[str, pd.Series],
    packet: Mapping[str, Any],
    cache: Any | None,
) -> Any:
    from app.prism.cache import cache_dir_from_env
    from app.prism.factors import build_factors

    macro = packet.get("macro") if isinstance(packet.get("macro"), Mapping) else {}
    yields = (macro or {}).get("yields") if isinstance((macro or {}).get("yields"), Mapping) else {}
    two_year = _finite(((yields or {}).get("DGS2") or {}).get("current"))
    risk_free = two_year / 100.0 if two_year is not None else None
    cache_dir = getattr(cache, "base_dir", None) or cache_dir_from_env()
    return build_factors(
        close,
        proxy_closes=dict(series_map),
        risk_free_annual=risk_free,
        cache_dir=cache_dir,
    )


def _build_regimes_section(
    packet: dict[str, Any], symbol: str, series_map: Mapping[str, pd.Series]
) -> pd.Series | None:
    """Build ``regimes`` and return the daily state-label series it decoded."""
    reference = series_map.get(REFERENCE_SYMBOL)
    ticker_close = series_map.get(symbol)
    if reference is None or len(reference) < 300:
        set_section(
            packet,
            "regimes",
            None,
            error=f"{REFERENCE_SYMBOL} history is required to train the regime model",
        )
        return None

    section = _guard(
        packet,
        "regimes",
        lambda: _build_regimes(reference, ticker_close),
    )
    if section is None:
        return None
    try:
        # The label series handed to the scenario weighting, the walk-forward
        # prediction history and the eigen symmetry test must be the *filtered*
        # decoding: the Viterbi path smooths over the whole window, so day t's
        # smoothed label depends on data after t and would make every "skill"
        # measurement built on it a look-ahead.
        from app.prism.regimes import fit_regime_model, regime_filtered_state_series

        labels = regime_filtered_state_series(fit_regime_model(reference))
    except Exception as exc:  # noqa: BLE001 - the section still stands without it
        record_error(packet, "regimes.labels", f"daily regime labels unavailable: {exc}")
        return _labels_from_history(section)
    return labels


def _labels_from_history(section: Mapping[str, Any]) -> pd.Series | None:
    """Monthly-sampled fallback label series, forward filled."""
    history = section.get("history")
    if not isinstance(history, list) or not history:
        return None
    dates: list[Any] = []
    labels: list[str] = []
    for row in history:
        if isinstance(row, Mapping) and row.get("date") and row.get("label"):
            dates.append(row["date"])
            labels.append(str(row["label"]))
    if not dates:
        return None
    return pd.Series(labels, index=pd.to_datetime(pd.Index(dates)), name="regime")


def _build_regimes(reference: pd.Series, ticker_close: pd.Series | None) -> Any:
    from app.prism.regimes import build_regimes

    section = build_regimes(reference, ticker_close, trained_on=REFERENCE_SYMBOL)
    if section.get("error"):
        raise ValueError(str(section["error"]))
    return section


def _build_entropy(close: pd.Series) -> Any:
    from app.prism.entropy import build_entropy

    return build_entropy(close)


def _build_spectral(close: pd.Series) -> Any:
    from app.prism.spectral import build_spectral

    return build_spectral(close)


def _build_volatility(
    close: pd.Series,
    client: Any,
    symbol: str,
    regime_labels: pd.Series | None,
    as_of: str,
) -> Any:
    from app.prism.volatility import build_volatility

    return build_volatility(
        close,
        client=client,
        ticker=symbol,
        regime_labels=regime_labels,
        as_of=date.fromisoformat(as_of),
    )


def _build_levels(
    history: Any,
    sec_trend: Mapping[str, Any] | None,
    profile: Mapping[str, Any],
    current_price: float | None,
) -> Any:
    from app.prism.levels import build_levels

    return build_levels(
        history,
        period=LEVELS_PERIOD,
        sec_trend=sec_trend,
        profile=dict(profile),
        current_price=current_price,
    )


def _build_scenarios(
    packet: Mapping[str, Any],
    close: pd.Series | None,
    current_price: float | None,
    regime_labels: pd.Series | None,
    symbol: str,
    series_map: Mapping[str, pd.Series] | None = None,
) -> Any:
    from app.prism.scenarios import build_scenarios

    relational = packet.get("relational") if isinstance(packet.get("relational"), Mapping) else {}
    seasonality = (
        packet.get("seasonality") if isinstance(packet.get("seasonality"), Mapping) else None
    )
    month_label = (seasonality or {}).get("month_label")
    # Use the volatility the packet already published rather than letting
    # `build_scenarios` compute a second one: the two used to disagree (log vs
    # simple returns) and the second one set the mixture's base sigma.
    realized_vol_annual: float | None = None
    volatility = packet.get("volatility")
    if isinstance(volatility, Mapping):
        realized = volatility.get("realized")
        one_year = realized.get("1y") if isinstance(realized, Mapping) else None
        if isinstance(one_year, Mapping):
            candidate = one_year.get("annualized")
            try:
                value = float(candidate) if candidate is not None else None
            except (TypeError, ValueError):
                value = None
            if value is not None and math.isfinite(value) and value > 0:
                realized_vol_annual = value
    # The shrinkage prior is the market's own long-run drift, taken from the
    # benchmark series the fan-out already loaded rather than re-fetched.
    market_close: pd.Series | None = None
    if series_map and symbol != REFERENCE_SYMBOL:
        candidate = series_map.get(REFERENCE_SYMBOL)
        if candidate is not None and len(candidate) > 60:
            market_close = candidate
    if market_close is None and symbol == REFERENCE_SYMBOL:
        # SPY is its own market: the prior is the same long-run drift.
        market_close = close
    section = build_scenarios(
        close=close,
        market_close=market_close,
        market_symbol=REFERENCE_SYMBOL,
        current_price=current_price,
        realized_vol_annual=realized_vol_annual,
        seasonality=seasonality,
        regimes=packet.get("regimes"),
        factors=packet.get("factors"),
        spectral=packet.get("spectral"),
        fundamentals=packet.get("fundamentals"),
        macro=packet.get("macro"),
        impact_weights=(relational or {}).get("impact_weights"),
        entropy=packet.get("entropy"),
        regime_label_series=regime_labels,
        ticker=symbol,
        month_label=str(month_label) if month_label else None,
    )
    if section.get("error"):
        raise ValueError(str(section["error"]))
    return section


def _build_eigen(
    packet: Mapping[str, Any],
    symbol: str,
    series_map: Mapping[str, pd.Series],
    regime_labels: pd.Series | None,
) -> Any:
    from app.prism.eigen import build_eigen
    from app.prism.relational import top_impact_symbols
    from app.prism.scenarios import make_weight_fn

    relational = packet.get("relational") if isinstance(packet.get("relational"), Mapping) else {}
    impact = top_impact_symbols(dict(relational or {}), limit=8) if relational else []
    signals, target = build_signal_frame(symbol, series_map, impact_symbols=impact)
    if signals.empty:
        raise ValueError("no monthly signal frame could be assembled")

    weight_fn = None
    predictions, realized = signal_prediction_history(signals, target)
    if not predictions.empty:
        # The signal frame is monthly (~112 rows). A 12-row holdout was below the
        # scoring floor inside `walk_forward_weights`, so no component was ever
        # evaluated, the weights collapsed to the flat prior and every
        # leave-one-out delta came out at exactly 0.0 — a perfect predictor
        # included. 36 monthly observations is a real out-of-sample window that
        # still leaves ~76 rows to fit on.
        weight_fn = make_weight_fn(
            predictions,
            realized,
            holdout_days=EIGEN_HOLDOUT_MONTHS,
            components=[str(name) for name in signals.columns],
        )

    monthly_labels: pd.Series | None = None
    if regime_labels is not None and len(regime_labels):
        aligned = pd.Series(regime_labels)
        if not isinstance(aligned.index, pd.DatetimeIndex):
            converted = pd.to_datetime(aligned.index, errors="coerce")
            aligned = aligned[converted.notna()]
            aligned.index = converted[converted.notna()]
        monthly_labels = aligned.resample("ME").last().reindex(signals.index).ffill()

    section = build_eigen(
        signals,
        target,
        regime_labels=monthly_labels,
        weight_fn=weight_fn,
        windows={"1y": 12, "6m": 6, "3m": 3},
        forward_days=1,
        # The windows count *monthly* observations, so the default 20-observation
        # floor made all three correlation columns null for every signal and left
        # `rank` as nothing but the input column order.
        ranking_min_observations=3,
    )
    section["signal_frequency"] = "monthly"
    section["weighting_basis"] = (
        "walk-forward out-of-sample R-squared of each signal's sign-conditioned "
        "expanding-mean forecast of next-month return"
    )
    if section.get("error"):
        raise ValueError(str(section["error"]))
    return section


def _build_memo(
    packet: Mapping[str, Any],
    text_generator: Any | None,
    api_key: str | None,
    text_model: str | None,
) -> Any:
    from app.prism.memo import build_memo

    return build_memo(
        packet,
        text_generator=text_generator,
        api_key=api_key,
        text_model=text_model,
    )


# --------------------------------------------------------------------------
# Reading stored packets and projecting them for agents
# --------------------------------------------------------------------------


def get_prism_packet(
    ticker: str,
    as_of: date | str | None = None,
    *,
    store: Any | None = None,
) -> dict[str, Any] | None:
    """Read the latest stored packet for ``ticker`` (``None`` when there is none)."""
    from app.prism.store import load_packet

    return load_packet(ticker, as_of, store=store)


def prism_summary(packet: Mapping[str, Any], *, max_news: int = 5) -> dict[str, Any]:
    """The bounded projection an agent or a proxy should receive.

    Small enough to inline in a prompt, complete enough to answer "what does
    Prism think and on what evidence".
    """
    memo = packet.get("memo") if isinstance(packet.get("memo"), Mapping) else {}
    scenarios = packet.get("scenarios") if isinstance(packet.get("scenarios"), Mapping) else {}
    profile = packet.get("profile") if isinstance(packet.get("profile"), Mapping) else {}
    regimes = packet.get("regimes") if isinstance(packet.get("regimes"), Mapping) else {}
    entropy = packet.get("entropy") if isinstance(packet.get("entropy"), Mapping) else {}
    fundamentals = (
        packet.get("fundamentals") if isinstance(packet.get("fundamentals"), Mapping) else {}
    )
    volatility = packet.get("volatility") if isinstance(packet.get("volatility"), Mapping) else {}
    seasonality = (
        packet.get("seasonality") if isinstance(packet.get("seasonality"), Mapping) else {}
    )
    news = packet.get("news") if isinstance(packet.get("news"), Mapping) else {}
    meta = packet.get("meta") if isinstance(packet.get("meta"), Mapping) else {}

    subject = (seasonality or {}).get("ticker")
    this_month = (
        ((subject or {}).get("this_month") or {}).get("10y")
        if isinstance(subject, Mapping)
        else {}
    )

    cases = (scenarios or {}).get("cases")
    case_summary = {}
    if isinstance(cases, Mapping):
        for name, block in cases.items():
            if isinstance(block, Mapping):
                case_summary[name] = {
                    "probability": _finite(block.get("probability")),
                    "narrative": block.get("narrative"),
                }

    return {
        "ticker": packet.get("ticker"),
        "as_of": packet.get("as_of"),
        "generated_at": packet.get("generated_at"),
        "engine_version": packet.get("engine_version"),
        "name": (profile or {}).get("name"),
        "sector": (profile or {}).get("sector"),
        "industry": (profile or {}).get("industry"),
        "recommendation": (memo or {}).get("recommendation"),
        "one_line": ((memo or {}).get("recommendation") or {}).get("one_line"),
        "entry_price": (memo or {}).get("entry_price"),
        "fair_value": (memo or {}).get("fair_value"),
        "stop_or_reassess": (memo or {}).get("stop_or_reassess"),
        "exit_targets": (memo or {}).get("exit_targets") or [],
        "key_determinants": (memo or {}).get("key_determinants") or [],
        "priced_in": (memo or {}).get("priced_in") or [],
        "scenarios": {
            "probability_horizon": (scenarios or {}).get("probability_horizon"),
            "weights": (scenarios or {}).get("weights"),
            "cases": case_summary,
            "entry": (scenarios or {}).get("entry"),
            "timing": (scenarios or {}).get("timing"),
            "watch_signals": ((scenarios or {}).get("watch_signals") or [])[:5],
        },
        "regime": (regimes or {}).get("current"),
        "entropy_3m": ((entropy or {}).get("windows") or {}).get("3m"),
        "seasonality_this_month": {
            "month": (seasonality or {}).get("month_label"),
            "ticker_10y": this_month,
        },
        "fundamentals": {
            "stage": (fundamentals or {}).get("stage"),
            "growth": (fundamentals or {}).get("growth"),
            "ratios": {
                key: ((fundamentals or {}).get("ratios") or {}).get(key)
                for key in ("pe", "ps", "pb", "ev_ebitda", "fcf_yield", "debt_to_equity")
            },
        },
        "volatility": {
            "realized_1y": ((volatility or {}).get("realized") or {}).get("1y"),
            "implied_atm": ((volatility or {}).get("implied") or {}).get("atm_iv"),
        },
        "news": [
            {
                "title": item.get("title"),
                "category": item.get("category"),
                "url": item.get("url"),
                "published": item.get("published"),
            }
            for item in ((news or {}).get("items") or [])[: max(1, int(max_news))]
            if isinstance(item, Mapping)
        ],
        "memo_excerpt": str((memo or {}).get("text") or "")[:1500],
        "unavailable_sections": [
            name
            for name in (
                "profile",
                "seasonality",
                "macro",
                "relational",
                "factors",
                "regimes",
                "entropy",
                "spectral",
                "eigen",
                "fundamentals",
                "filings",
                "volatility",
                "levels",
                "news",
                "scenarios",
                "recent",
                "memo",
            )
            if packet.get(name) is None
        ],
        "errors": (meta or {}).get("errors") or [],
        "disclaimer": "Research only. This is not investment advice and no order was placed.",
    }
