from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol, cast

import pandas as pd
import requests
import yfinance as yf

from app._perf import TTLCache, tune_session

REQUIRED_COLUMNS = ("Open", "High", "Low", "Close", "Volume")
_HISTORY_TTL_SECONDS = 90.0
_SNAPSHOT_TTL_SECONDS = 5.0
_PROFILE_TTL_SECONDS = 90.0
_SEARCH_TTL_SECONDS = 60.0
_OPTIONS_TTL_SECONDS = 30.0
_SEARCH_CONCURRENCY_PER_WORKER = 2
_EASTERN = "America/New_York"
HISTORY_INTERVALS = ("15m", "1d", "1w")
_HISTORY_INTERVAL_ALIASES = {
    "15m": "15m",
    "15min": "15m",
    "15": "15m",
    "1d": "1d",
    "d": "1d",
    "day": "1d",
    "daily": "1d",
    "1w": "1w",
    "w": "1w",
    "wk": "1w",
    "1wk": "1w",
    "week": "1w",
    "weekly": "1w",
}
_MASSIVE_INTERVALS = {
    "15m": (15, "minute"),
    "1d": (1, "day"),
    "1w": (1, "week"),
}
_YFINANCE_INTERVALS = {
    "15m": "15m",
    "1d": "1d",
    "1w": "1wk",
}
_DEFAULT_PERIOD_FOR_INTERVAL = {
    "15m": "5d",
    "1d": "1y",
    "1w": "2y",
}
_INTRADAY_PERIODS = frozenset({"1d", "5d", "1mo"})

MAX_SEARCH_QUERY_LENGTH = 100
MAX_SECURITY_SYMBOL_LENGTH = 32
# Kept as the legacy value for callers that import this constant. The live
# response uses the selected client's provider label when Massive is configured.
SEARCH_PROVIDER = "Yahoo Finance via yfinance"
SECURITY_SYMBOL_PATTERN = r"^(?:[A-Z0-9][A-Z0-9.-]{0,31}|\^[A-Z0-9][A-Z0-9.-]{0,30})$"
_SECURITY_SYMBOL_RE = re.compile(SECURITY_SYMBOL_PATTERN)
_SEARCH_ASSET_TYPES = {
    "EQUITY": "equity",
    "ETF": "etf",
    "MUTUALFUND": "mutual_fund",
    "INDEX": "index",
    "CRYPTOCURRENCY": "crypto",
}


class MarketDataError(RuntimeError):
    """Raised when no provider can return usable market data."""


class MarketDataBusyError(MarketDataError):
    """Raised when the bounded provider search capacity is already in use."""


class MarketDataCapabilityError(MarketDataError):
    """Raised when the configured subscription/provider does not expose a dataset."""


@dataclass(frozen=True)
class HistoryResult:
    ticker: str
    data: pd.DataFrame
    provider: str
    note: str
    interval: str = "1d"


@dataclass(frozen=True)
class OptionChainResult:
    ticker: str
    expiry: str
    current_price: float
    rows: list[dict[str, Any]]
    expirations: list[str]
    provider: str
    note: str


class MarketDataProvider(Protocol):
    name: str
    note: str

    def get_history(
        self, ticker: str, *, start: date, end: date, interval: str
    ) -> HistoryResult: ...

    def get_profile(self, ticker: str) -> dict[str, Any]: ...

    def search_securities(self, query: str, *, limit: int) -> list[dict[str, str]]: ...

    def get_option_chain(self, ticker: str, expiry: str | None = None) -> OptionChainResult: ...

    def get_expirations(self, ticker: str) -> list[str]: ...

    def get_snapshot(self, ticker: str, *, asset_class: str = "stocks") -> dict[str, Any]: ...

    def get_aggregates(
        self,
        ticker: str,
        *,
        multiplier: int,
        timespan: str,
        start: str,
        end: str,
        asset_class: str = "stocks",
    ) -> dict[str, Any]: ...

    def get_trades(
        self, ticker: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    def get_quotes(
        self, ticker: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    def get_contracts(
        self, ticker: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    def get_events(
        self, ticker: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    def get_dividends(
        self, ticker: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    def get_splits(
        self, ticker: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    def get_financials(
        self, ticker: str, *, statement: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    def get_market_status(self) -> dict[str, Any]: ...

    def get_news(
        self, ticker: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    def get_corporate_events(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def get_ipos(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def get_conditions(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def get_all_snapshot(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def get_option_snapshot(self, underlying: str, contract: str) -> dict[str, Any]: ...


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int = 100) -> int:
    try:
        return max(minimum, min(maximum, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


def today_utc() -> date:
    return datetime.now(UTC).date()


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def dates_for_period(period: str) -> tuple[date, date]:
    end = today_utc() + timedelta(days=1)
    days_by_period = {
        "1d": 7,
        "5d": 14,
        "1w": 14,
        "1mo": 45,
        "3mo": 120,
        "6mo": 220,
        "1y": 400,
        "2y": 800,
        "5y": 1900,
        "10y": 3800,
        "max": 8000,
    }
    return end - timedelta(days=days_by_period.get(period, 400)), end


def normalize_history_interval(value: str | None) -> str:
    raw = str(value or "1d").strip().lower()
    interval = _HISTORY_INTERVAL_ALIASES.get(raw)
    if interval is None:
        raise ValueError("interval must be 15m, 1d, or 1w")
    return interval


def massive_interval_spec(interval: str) -> tuple[int, str]:
    return _MASSIVE_INTERVALS[normalize_history_interval(interval)]


def yfinance_interval(interval: str) -> str:
    return _YFINANCE_INTERVALS[normalize_history_interval(interval)]


def default_period_for_interval(interval: str) -> str:
    return _DEFAULT_PERIOD_FOR_INTERVAL[normalize_history_interval(interval)]


def clamp_period_for_interval(period: str, interval: str) -> str:
    normalized = normalize_history_interval(interval)
    if normalized == "15m" and period not in _INTRADAY_PERIODS:
        return "5d"
    return period


def chart_history_options(
    payload: dict[str, Any],
    *,
    default_period: str | None = None,
    include_range: bool = False,
) -> dict[str, Any]:
    interval = normalize_history_interval(payload.get("interval"))
    period = clamp_period_for_interval(
        str(payload.get("period") or default_period or default_period_for_interval(interval)),
        interval,
    )
    options: dict[str, Any] = {"period": period, "interval": interval}
    if include_range:
        if payload.get("start_date"):
            options["start"] = payload.get("start_date")
        if payload.get("end_date"):
            options["end"] = payload.get("end_date")
    return options


def series_timestamp_label(timestamp: Any, interval: str = "1d") -> str:
    ts = pd.Timestamp(timestamp)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    if normalize_history_interval(interval) == "15m":
        return ts.strftime("%Y-%m-%dT%H:%M:%S")
    return ts.date().isoformat()


def normalize_ohlcv(data: pd.DataFrame) -> pd.DataFrame:
    if data.empty:
        return data

    normalized = data.copy()
    if isinstance(normalized.columns, pd.MultiIndex):
        level_zero = [str(value) for value in normalized.columns.get_level_values(0)]
        level_one = [str(value) for value in normalized.columns.get_level_values(1)]
        if any(column in REQUIRED_COLUMNS for column in level_zero):
            normalized.columns = normalized.columns.get_level_values(0)
        elif any(column in REQUIRED_COLUMNS for column in level_one):
            normalized.columns = normalized.columns.get_level_values(1)
        else:
            normalized.columns = [
                " ".join(str(part) for part in column if part) for column in normalized.columns
            ]

    normalized = normalized.rename(
        columns={column: str(column).strip().title() for column in normalized.columns}
    )
    if "Adj Close" not in normalized.columns and "Close" in normalized.columns:
        normalized["Adj Close"] = normalized["Close"]

    keep = [column for column in (*REQUIRED_COLUMNS, "Adj Close") if column in normalized.columns]
    normalized = normalized[keep]
    normalized.index = pd.to_datetime(normalized.index).tz_localize(None)

    for column in keep:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")

    normalized = normalized.sort_index().dropna(subset=["Open", "High", "Low", "Close"])
    if "Volume" in normalized.columns:
        normalized["Volume"] = normalized["Volume"].fillna(0)
    return normalized


class LegacyMarketDataProvider:
    """Temporary yfinance/Nasdaq adapter kept behind the facade."""

    name = "yfinance"
    note = "Yahoo Finance via yfinance; Nasdaq daily-history fallback"

    def __init__(self, session: requests.Session) -> None:
        self.session = session

    def get_history(self, ticker: str, *, start: date, end: date, interval: str) -> HistoryResult:
        resolved = normalize_history_interval(interval)
        data = yf.download(
            ticker,
            start=start.isoformat(),
            end=end.isoformat(),
            period=None,
            interval=yfinance_interval(resolved),
            auto_adjust=False,
            progress=False,
            threads=False,
        )
        normalized = normalize_ohlcv(data)
        if not normalized.empty:
            return HistoryResult(
                ticker, normalized, self.name, "Yahoo Finance via yfinance", interval=resolved
            )
        if resolved != "1d":
            return HistoryResult(
                ticker, normalized, self.name, "Yahoo Finance via yfinance", interval=resolved
            )
        nasdaq = self._history_from_nasdaq(ticker, start=start, end=end)
        return replace(nasdaq, interval=resolved)

    def _history_from_nasdaq(self, ticker: str, *, start: date, end: date) -> HistoryResult:
        nasdaq_symbol = to_nasdaq_symbol(ticker)
        response = self.session.get(
            f"https://api.nasdaq.com/api/quote/{nasdaq_symbol}/historical",
            params={
                "assetclass": "stocks",
                "fromdate": start.isoformat(),
                "todate": end.isoformat(),
                "limit": "9999",
            },
            headers={
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://www.nasdaq.com",
                "Referer": "https://www.nasdaq.com/",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
                ),
            },
            timeout=15,
        )
        response.raise_for_status()
        rows = response.json().get("data", {}).get("tradesTable", {}).get("rows", [])
        if not rows:
            raise MarketDataError(f"Nasdaq returned no rows for {nasdaq_symbol}")

        frame = pd.DataFrame(
            [
                {
                    "Date": datetime.strptime(row["date"], "%m/%d/%Y"),
                    "Open": parse_market_number(row["open"]),
                    "High": parse_market_number(row["high"]),
                    "Low": parse_market_number(row["low"]),
                    "Close": parse_market_number(row["close"]),
                    "Volume": parse_market_number(row["volume"]),
                }
                for row in rows
            ]
        ).set_index("Date")
        frame["Adj Close"] = frame["Close"]
        return HistoryResult(
            ticker,
            normalize_ohlcv(frame),
            "nasdaq",
            "Nasdaq public historical endpoint fallback",
        )

    def get_profile(self, ticker: str) -> dict[str, Any]:
        try:
            info = yf.Ticker(ticker).get_info()
            return info if isinstance(info, dict) else {}
        except Exception:
            return {}

    def search_securities(self, query: str, *, limit: int) -> list[dict[str, str]]:
        search = yf.Search(
            query,
            max_results=limit,
            news_count=0,
            lists_count=0,
            include_cb=False,
            include_nav_links=False,
            include_research=False,
            include_cultural_assets=False,
            enable_fuzzy_query=True,
            timeout=10,
        )
        quotes = search.quotes if isinstance(search.quotes, list) else []
        results: list[dict[str, str]] = []
        seen_symbols: set[str] = set()
        for quote in quotes:
            if not isinstance(quote, dict):
                continue
            asset_type = _SEARCH_ASSET_TYPES.get(str(quote.get("quoteType", "")).upper())
            symbol = str(quote.get("symbol", "")).strip().upper()
            if (
                not asset_type
                or not _SECURITY_SYMBOL_RE.fullmatch(symbol)
                or symbol in seen_symbols
            ):
                continue
            seen_symbols.add(symbol)
            results.append(
                {
                    "symbol": symbol,
                    "name": str(quote.get("longname") or quote.get("shortname") or "").strip(),
                    "exchange": str(quote.get("exchDisp") or quote.get("exchange") or "").strip(),
                    "asset_type": asset_type,
                }
            )
        return results

    def get_option_chain(self, ticker: str, expiry: str | None = None) -> OptionChainResult:
        stock = yf.Ticker(ticker)
        history = stock.history(period="5d")
        if history.empty:
            raise MarketDataError(f"No recent price data for {ticker}")
        expiries = list(stock.options)
        selected_expiry = choose_expiry(expiries, expiry)
        chain = stock.option_chain(selected_expiry)
        rows = option_rows(chain.calls, chain.puts, float(history["Close"].dropna().iloc[-1]))
        if not rows:
            raise MarketDataError(f"No usable option rows for {ticker} {selected_expiry}")
        current_price = float(history["Close"].dropna().iloc[-1])
        return OptionChainResult(
            ticker, selected_expiry, current_price, rows, expiries, self.name, self.note
        )

    def get_expirations(self, ticker: str) -> list[str]:
        return list(yf.Ticker(ticker).options)

    def _unsupported(self, dataset: str) -> None:
        raise MarketDataCapabilityError(f"Legacy provider does not support Massive {dataset}")

    def get_snapshot(self, _ticker: str, *, asset_class: str = "stocks") -> dict[str, Any]:
        _ = asset_class
        self._unsupported("snapshot")
        return {}

    def get_aggregates(self, _ticker: str, **_: Any) -> dict[str, Any]:
        self._unsupported("aggregates")
        return {}

    def get_trades(self, _ticker: str, **_: Any) -> dict[str, Any]:
        self._unsupported("trades")
        return {}

    def get_quotes(self, _ticker: str, **_: Any) -> dict[str, Any]:
        self._unsupported("quotes")
        return {}

    def get_contracts(self, _ticker: str, **_: Any) -> dict[str, Any]:
        self._unsupported("contracts")
        return {}

    def get_events(self, _ticker: str, **_: Any) -> dict[str, Any]:
        self._unsupported("events")
        return {}

    def get_dividends(self, _ticker: str, **_: Any) -> dict[str, Any]:
        self._unsupported("dividends")
        return {}

    def get_splits(self, _ticker: str, **_: Any) -> dict[str, Any]:
        self._unsupported("splits")
        return {}

    def get_financials(self, _ticker: str, **_: Any) -> dict[str, Any]:
        self._unsupported("financials")
        return {}

    def get_market_status(self) -> dict[str, Any]:
        self._unsupported("market status")
        return {}

    def get_news(self, _ticker: str, **_: Any) -> dict[str, Any]:
        self._unsupported("news")
        return {}

    def get_corporate_events(self, **_: Any) -> dict[str, Any]:
        self._unsupported("TMX corporate events")
        return {}

    def get_ipos(self, **_: Any) -> dict[str, Any]:
        self._unsupported("IPOs")
        return {}

    def get_conditions(self, **_: Any) -> dict[str, Any]:
        self._unsupported("market conditions")
        return {}

    def get_all_snapshot(self, **_: Any) -> dict[str, Any]:
        self._unsupported("full-market snapshot")
        return {}

    def get_option_snapshot(self, _underlying: str, _contract: str) -> dict[str, Any]:
        self._unsupported("option contract snapshot")
        return {}


class MarketDataClient:
    """Stable application facade over Massive and the temporary legacy fallback."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        provider: MarketDataProvider | None = None,
        fallback_provider: MarketDataProvider | None = None,
        fallback_enabled: bool | None = None,
    ) -> None:
        self.session = session or requests.Session()
        tune_session(self.session, pool_maxsize=32)
        self.provider = provider or self._build_massive_provider()
        self.fallback_provider = fallback_provider or LegacyMarketDataProvider(self.session)
        self.fallback_enabled = (
            _env_bool("MARKET_DATA_FALLBACK_ENABLED", True)
            if fallback_enabled is None
            else fallback_enabled
        )
        self._history_cache: TTLCache = TTLCache(_HISTORY_TTL_SECONDS)
        self._snapshot_cache: TTLCache = TTLCache(_SNAPSHOT_TTL_SECONDS)
        self._profile_cache: TTLCache = TTLCache(_PROFILE_TTL_SECONDS)
        self._search_cache: TTLCache = TTLCache(_SEARCH_TTL_SECONDS)
        self._options_cache: TTLCache = TTLCache(_OPTIONS_TTL_SECONDS)
        self._search_slots = threading.BoundedSemaphore(_SEARCH_CONCURRENCY_PER_WORKER)
        self._provider_state = threading.local()

    @staticmethod
    def _build_massive_provider() -> MarketDataProvider:
        from app.massive import MassiveProvider

        return cast(MarketDataProvider, MassiveProvider.from_env())

    @property
    def provider_label(self) -> str:
        active = getattr(self._provider_state, "provider", self.provider)
        return getattr(active, "name", "massive")

    @property
    def provider_note(self) -> str:
        active = getattr(self._provider_state, "provider", self.provider)
        return getattr(active, "note", "Massive market data")

    def _set_active_provider(self, provider: Any) -> None:
        self._provider_state.provider = provider

    def _call_provider(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            result = getattr(self.provider, method)(*args, **kwargs)
            self._set_active_provider(self.provider)
            return result
        except (TypeError, ValueError):
            raise
        except MarketDataCapabilityError:
            if not self.fallback_enabled:
                raise
        except Exception as exc:
            if not self.fallback_enabled:
                raise MarketDataError(f"{self.provider_label} {method} failed: {exc}") from exc
        try:
            result = getattr(self.fallback_provider, method)(*args, **kwargs)
            self._set_active_provider(self.fallback_provider)
            return result
        except MarketDataError:
            raise
        except Exception as exc:
            raise MarketDataError(f"No provider could satisfy {method}: {exc}") from exc

    def get_history(
        self,
        ticker: str,
        *,
        period: str = "1y",
        start: str | date | None = None,
        end: str | date | None = None,
        interval: str = "1d",
    ) -> HistoryResult:
        symbol = clean_ticker(ticker)
        resolved_interval = normalize_history_interval(interval)
        start_date = coerce_date(start)
        end_date = coerce_date(end)
        if start_date is None or end_date is None:
            period_start, period_end = dates_for_period(period)
            start_date = start_date or period_start
            end_date = end_date or period_end
        cache_key = (symbol, period, start_date, end_date, resolved_interval)
        cached = self._history_cache.get(cache_key)
        if cached is not None:
            return self._with_live_quote(cached, resolved_interval)
        try:
            result = self.provider.get_history(
                symbol, start=start_date, end=end_date, interval=resolved_interval
            )
        except Exception as exc:
            if not self.fallback_enabled:
                raise MarketDataError(f"No historical data for {symbol}. {exc}") from exc
            try:
                result = self._history_from_yfinance(
                    symbol,
                    period=period,
                    start=start_date,
                    end=end_date,
                    interval=resolved_interval,
                )
            except Exception as fallback_exc:
                raise MarketDataError(
                    f"No historical data for {symbol}. massive: {exc}; legacy: {fallback_exc}"
                ) from fallback_exc
        if (
            self.fallback_enabled
            and result.provider == getattr(self.provider, "name", "massive")
            and (result.data.empty or history_coverage_is_short(result.data, start_date, end_date))
        ):
            try:
                result = self._history_from_yfinance(
                    symbol,
                    period=period,
                    start=start_date,
                    end=end_date,
                    interval=resolved_interval,
                )
            except Exception as fallback_exc:
                raise MarketDataError(
                    f"No historical data for {symbol}. massive returned incomplete data; "
                    f"legacy: {fallback_exc}"
                ) from fallback_exc
        if result.interval != resolved_interval:
            result = replace(result, interval=resolved_interval)
        if result.provider == getattr(
            self.provider, "name", "massive"
        ) and history_coverage_is_short(result.data, start_date, end_date):
            result = replace(
                result,
                note=f"{result.note}; returned history is shorter than requested coverage",
            )
        if result.data.empty:
            raise MarketDataError(f"No historical data for {symbol}. providers returned no rows")
        self._set_active_provider(
            self.provider
            if result.provider == getattr(self.provider, "name", "massive")
            else self.fallback_provider
        )
        self._history_cache.set(cache_key, result)
        return self._with_live_quote(result, resolved_interval)

    def _history_from_yfinance(
        self,
        ticker: str,
        *,
        period: str,
        start: date,
        end: date,
        interval: str,
    ) -> HistoryResult:
        """Compatibility seam for tests and callers that patched the old fallback."""
        _ = period
        return self.fallback_provider.get_history(ticker, start=start, end=end, interval=interval)

    def _with_live_quote(self, result: HistoryResult, interval: str) -> HistoryResult:
        try:
            snapshot = self.get_snapshot(result.ticker)
        except Exception:
            return result
        updated = apply_live_quote(result.data, snapshot, interval)
        if updated is result.data:
            return result
        return replace(
            result,
            data=updated,
            interval=interval,
            note=f"{result.note}; last bar includes live snapshot quote",
        )

    def get_profile(self, ticker: str) -> dict[str, Any]:
        symbol = clean_ticker(ticker)
        cached = self._profile_cache.get(symbol)
        if cached is not None:
            return cached
        try:
            profile = self._call_provider("get_profile", symbol)
        except Exception:
            profile = {}
        profile = profile if isinstance(profile, dict) else {}
        if self.provider_label == "massive" and self.fallback_enabled:
            try:
                legacy_profile = self.fallback_provider.get_profile(symbol)
            except Exception:
                legacy_profile = {}
            if isinstance(legacy_profile, dict):
                filled = [
                    key
                    for key, value in legacy_profile.items()
                    if key not in profile or profile.get(key) in (None, "")
                    if value not in (None, "")
                ]
                for key in filled:
                    profile[key] = legacy_profile[key]
                if filled:
                    profile["provider_fields_fallback"] = "yfinance"
        self._profile_cache.set(symbol, profile)
        return profile

    def search_securities(self, query: str, *, limit: int = 8) -> list[dict[str, str]]:
        cleaned_query = query.strip()
        if not cleaned_query:
            raise ValueError("Search query is required")
        if len(cleaned_query) > MAX_SEARCH_QUERY_LENGTH:
            raise ValueError(f"Search query must be at most {MAX_SEARCH_QUERY_LENGTH} characters")
        if not 1 <= limit <= 10:
            raise ValueError("Search limit must be between 1 and 10")
        cache_key = (cleaned_query.casefold(), limit)
        cached = self._search_cache.get(cache_key)
        if cached is not None:
            return cached
        if not self._search_slots.acquire(blocking=False):
            raise MarketDataBusyError("Security search is busy; try again shortly")
        try:
            try:
                results = self._call_provider("search_securities", cleaned_query, limit=limit)
            except Exception as exc:
                raise MarketDataError(f"Security search failed: {exc}") from exc
        finally:
            self._search_slots.release()
        self._search_cache.set(cache_key, results)
        return results

    def get_option_chain(self, ticker: str, expiry: str | None = None) -> OptionChainResult:
        symbol = clean_ticker(ticker)
        if expiry:
            parse_expiry(expiry)
        cache_key = (symbol, expiry)
        cached = self._options_cache.get(cache_key)
        if cached is not None:
            return cached
        result = self._call_provider("get_option_chain", symbol, expiry)
        self._options_cache.set(cache_key, result)
        return result

    def get_expirations(self, ticker: str) -> list[str]:
        return self._call_provider("get_expirations", clean_ticker(ticker))

    def get_snapshot(self, ticker: str, *, asset_class: str = "stocks") -> dict[str, Any]:
        symbol = clean_ticker(ticker)
        cache_key = (symbol, asset_class)
        cached = self._snapshot_cache.get(cache_key)
        if cached is not None:
            return cached
        payload = self._call_provider("get_snapshot", symbol, asset_class=asset_class)
        self._snapshot_cache.set(cache_key, payload)
        return payload

    def get_aggregates(
        self,
        ticker: str,
        *,
        multiplier: int = 1,
        timespan: str = "day",
        start: str,
        end: str,
        asset_class: str = "stocks",
    ) -> dict[str, Any]:
        if multiplier < 1:
            raise ValueError("multiplier must be at least 1")
        if timespan not in {"minute", "hour", "day", "week", "month", "quarter", "year"}:
            raise ValueError("timespan is not supported")
        if parse_date(start) is None or parse_date(end) is None:
            raise ValueError("start and end must use YYYY-MM-DD")
        return self._call_provider(
            "get_aggregates",
            clean_ticker(ticker),
            multiplier=multiplier,
            timespan=timespan,
            start=start,
            end=end,
            asset_class=asset_class,
        )

    def get_trades(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._call_provider("get_trades", clean_ticker(ticker), params=params)

    def get_quotes(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._call_provider("get_quotes", clean_ticker(ticker), params=params)

    def get_contracts(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._call_provider("get_contracts", clean_ticker(ticker), params=params)

    def get_events(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._call_provider("get_events", clean_ticker(ticker), params=params)

    def get_dividends(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._call_provider("get_dividends", clean_ticker(ticker), params=params)

    def get_splits(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._call_provider("get_splits", clean_ticker(ticker), params=params)

    def get_financials(
        self,
        ticker: str,
        *,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._call_provider(
            "get_financials", clean_ticker(ticker), statement=statement, params=params
        )

    def get_market_status(self) -> dict[str, Any]:
        return self._call_provider("get_market_status")

    def get_news(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._call_provider("get_news", clean_ticker(ticker), params=params)

    def get_corporate_events(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._call_provider("get_corporate_events", params=params)

    def get_ipos(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._call_provider("get_ipos", params=params)

    def get_conditions(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._call_provider("get_conditions", params=params)

    def get_all_snapshot(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._call_provider("get_all_snapshot", params=params)

    def get_option_snapshot(self, underlying: str, contract: str) -> dict[str, Any]:
        return self._call_provider(
            "get_option_snapshot", clean_ticker(underlying), contract.strip().upper()
        )


def clean_ticker(ticker: str) -> str:
    cleaned = ticker.strip().upper()
    if not cleaned:
        raise ValueError("Ticker is required")
    if not _SECURITY_SYMBOL_RE.fullmatch(cleaned):
        raise ValueError("Ticker contains unsupported characters")
    return cleaned


def history_coverage_is_short(data: pd.DataFrame, start: date, end: date) -> bool:
    if data.empty:
        return True
    first = pd.Timestamp(data.index.min()).date()
    requested_days = max(1, (end - start).days)
    gap_days = max(0, (first - start).days)
    return requested_days >= 365 and gap_days > 90


def coerce_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return parse_date(value)


def parse_market_number(value: str | int | float | None) -> float:
    if value is None:
        return float("nan")
    if isinstance(value, int | float):
        return float(value)
    cleaned = value.replace("$", "").replace(",", "").strip()
    if cleaned in {"", "N/A"}:
        return float("nan")
    return float(cleaned)


def to_nasdaq_symbol(ticker: str) -> str:
    if ticker.startswith("^"):
        raise MarketDataError("Nasdaq fallback only supports listed equity symbols")
    return ticker.upper().replace("-", ".")


def choose_expiry(expiries: list[str], expiry: str | None) -> str:
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


def apply_live_quote(data: pd.DataFrame, snapshot: dict[str, Any], interval: str) -> pd.DataFrame:
    quote = snapshot_quote(snapshot)
    if quote is None:
        return data
    resolved = normalize_history_interval(interval)
    trade_ts = _as_naive_utc(quote["ts_ms"]) or pd.Timestamp.utcnow().tz_localize(None)
    bucket = _session_bucket(trade_ts, resolved)
    open_px = quote["open"]
    high_px = max(quote["high"], quote["price"])
    low_px = min(quote["low"], quote["price"])
    close_px = quote["price"]
    volume = quote["volume"]
    if data.empty:
        frame = pd.DataFrame(
            {
                "Open": [open_px],
                "High": [high_px],
                "Low": [low_px],
                "Close": [close_px],
                "Volume": [volume],
                "Adj Close": [close_px],
            },
            index=[bucket],
        )
        frame.index.name = "Date"
        return frame

    frame = data.copy()
    last_index = pd.Timestamp(frame.index[-1])
    if _same_bar(last_index, bucket, resolved):
        row = frame.iloc[-1]
        frame.iloc[-1, frame.columns.get_loc("High")] = max(float(row["High"]), high_px, close_px)
        frame.iloc[-1, frame.columns.get_loc("Low")] = min(float(row["Low"]), low_px, close_px)
        frame.iloc[-1, frame.columns.get_loc("Close")] = close_px
        if "Adj Close" in frame.columns:
            frame.iloc[-1, frame.columns.get_loc("Adj Close")] = close_px
        if "Volume" in frame.columns and volume:
            frame.iloc[-1, frame.columns.get_loc("Volume")] = max(float(row["Volume"]), volume)
        return frame

    if bucket <= last_index:
        return data

    added = pd.DataFrame(
        {
            "Open": [close_px if resolved == "15m" else open_px],
            "High": [high_px],
            "Low": [low_px],
            "Close": [close_px],
            "Volume": [volume],
            "Adj Close": [close_px],
        },
        index=[bucket],
    )
    combined = pd.concat([frame, added])
    combined.index.name = frame.index.name
    return combined


def snapshot_quote(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    payload = snapshot.get("ticker")
    if not isinstance(payload, dict):
        results = snapshot.get("results")
        if isinstance(results, dict):
            payload = results
        elif isinstance(results, list) and results and isinstance(results[0], dict):
            payload = results[0]
        elif isinstance(snapshot.get("day"), dict) or isinstance(snapshot.get("lastTrade"), dict):
            payload = snapshot
        else:
            return None
    last_trade = payload.get("lastTrade") or payload.get("last_trade") or {}
    day = payload.get("day") if isinstance(payload.get("day"), dict) else {}
    minute = payload.get("min") if isinstance(payload.get("min"), dict) else payload.get("minute")
    minute = minute if isinstance(minute, dict) else {}
    price = last_trade.get("p") or last_trade.get("price") or day.get("c") or minute.get("c")
    if price is None:
        return None
    return {
        "price": float(price),
        "open": float(day.get("o") or minute.get("o") or price),
        "high": float(day.get("h") or minute.get("h") or price),
        "low": float(day.get("l") or minute.get("l") or price),
        "volume": float(day.get("v") or minute.get("v") or 0.0),
        "ts_ms": last_trade.get("t") or day.get("t") or minute.get("t"),
    }


def epoch_to_naive_utc(value: Any) -> pd.Timestamp:
    if isinstance(value, bool) or value is None:
        raise ValueError("epoch timestamp is required")
    if isinstance(value, int | float):
        numeric: int | float = value
    else:
        item = getattr(value, "item", None)
        numeric = item() if callable(item) else float(value)
    if isinstance(numeric, bool) or not isinstance(numeric, int | float):
        raise ValueError("epoch timestamp must be numeric")
    magnitude = abs(float(numeric))
    if magnitude >= 1e16:
        unit = "ns"
    elif magnitude >= 1e13:
        unit = "us"
    elif magnitude >= 1e11:
        unit = "ms"
    else:
        unit = "s"
    return pd.Timestamp(int(numeric), unit=unit, tz="UTC").tz_localize(None)


def _as_naive_utc(value: Any) -> pd.Timestamp | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float) or callable(getattr(value, "item", None)):
        try:
            return epoch_to_naive_utc(value)
        except (OverflowError, TypeError, ValueError, pd.errors.OutOfBoundsDatetime):
            return None
    ts = pd.Timestamp(value)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return ts.tz_localize(None)


def _session_bucket(ts: pd.Timestamp, interval: str) -> pd.Timestamp:
    aware = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    eastern = aware.tz_convert(_EASTERN)
    if interval == "15m":
        return eastern.floor("15min").tz_convert("UTC").tz_localize(None)
    if interval == "1w":
        monday = eastern.normalize() - pd.Timedelta(days=int(eastern.dayofweek))
        return monday.tz_convert("UTC").tz_localize(None)
    return eastern.normalize().tz_convert("UTC").tz_localize(None)


def _same_bar(existing: pd.Timestamp, incoming: pd.Timestamp, interval: str) -> bool:
    left = pd.Timestamp(existing)
    if interval == "15m":
        return abs((left - incoming).total_seconds()) < 60
    return _session_bucket(left, interval) == _session_bucket(incoming, interval)
