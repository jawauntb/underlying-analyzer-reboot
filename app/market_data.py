from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
import requests
import yfinance as yf

from app._perf import TTLCache, tune_session

REQUIRED_COLUMNS = ("Open", "High", "Low", "Close", "Volume")

# Short TTL: history/quote reads are idempotent but must not go stale during a live
# session. 90s coalesces the repeated per-ticker fan-out within a request/burst while
# still refreshing well inside a trading minute.
_HISTORY_TTL_SECONDS = 90.0
_PROFILE_TTL_SECONDS = 90.0
_SEARCH_TTL_SECONDS = 60.0
_SEARCH_CONCURRENCY_PER_WORKER = 2

MAX_SEARCH_QUERY_LENGTH = 100
MAX_SECURITY_SYMBOL_LENGTH = 32
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


@dataclass(frozen=True)
class HistoryResult:
    ticker: str
    data: pd.DataFrame
    provider: str
    note: str


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


class MarketDataClient:
    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        # Widen the connection pool so concurrent per-ticker fetches reuse keep-alive
        # sockets instead of churning new connections under fan-out.
        tune_session(self.session, pool_maxsize=32)
        self._history_cache: TTLCache = TTLCache(_HISTORY_TTL_SECONDS)
        self._profile_cache: TTLCache = TTLCache(_PROFILE_TTL_SECONDS)
        self._search_cache: TTLCache = TTLCache(_SEARCH_TTL_SECONDS)
        self._search_slots = threading.BoundedSemaphore(_SEARCH_CONCURRENCY_PER_WORKER)

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
        start_date = coerce_date(start)
        end_date = coerce_date(end)
        if start_date is None or end_date is None:
            period_start, period_end = dates_for_period(period)
            start_date = start_date or period_start
            end_date = end_date or period_end

        cache_key = (symbol, period, start_date, end_date, interval)
        cached = self._history_cache.get(cache_key)
        if cached is not None:
            return cached

        errors: list[str] = []
        try:
            result = self._history_from_yfinance(
                symbol,
                period=period,
                start=start_date,
                end=end_date,
                interval=interval,
            )
            if not result.data.empty:
                self._remember_history(cache_key, result)
                return result
        except Exception as exc:
            errors.append(f"yfinance: {exc}")

        if interval == "1d":
            try:
                result = self._history_from_nasdaq(symbol, start=start_date, end=end_date)
                if not result.data.empty:
                    self._remember_history(cache_key, result)
                    return result
            except Exception as exc:
                errors.append(f"nasdaq: {exc}")

        message = "; ".join(errors) if errors else "providers returned no rows"
        raise MarketDataError(f"No historical data for {symbol}. {message}")

    def get_profile(self, ticker: str) -> dict[str, Any]:
        symbol = clean_ticker(ticker)
        cached = self._profile_cache.get(symbol)
        if cached is not None:
            return cached

        profile: dict[str, Any] = {}
        try:
            stock = yf.Ticker(symbol)
            info = stock.get_info()
            if isinstance(info, dict):
                profile = info
        except Exception:
            profile = {}

        self._profile_cache.set(symbol, profile)
        return profile

    def search_securities(self, query: str, *, limit: int = 8) -> list[dict[str, str]]:
        cleaned_query = query.strip()
        if not cleaned_query:
            raise ValueError("Search query is required")
        if len(cleaned_query) > MAX_SEARCH_QUERY_LENGTH:
            raise ValueError(
                f"Search query must be at most {MAX_SEARCH_QUERY_LENGTH} characters"
            )
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
                search = yf.Search(
                    cleaned_query,
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
            except Exception as exc:
                raise MarketDataError(f"Security search failed: {exc}") from exc
        finally:
            self._search_slots.release()

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

        self._search_cache.set(cache_key, results)
        return results

    def _remember_history(
        self, cache_key: tuple[str, str, date, date, str], result: HistoryResult
    ) -> None:
        self._history_cache.set(cache_key, result)

    def _history_from_yfinance(
        self,
        ticker: str,
        *,
        period: str,
        start: date,
        end: date,
        interval: str,
    ) -> HistoryResult:
        data = yf.download(
            ticker,
            start=start.isoformat(),
            end=end.isoformat(),
            period=None if start or end else period,
            interval=interval,
            auto_adjust=False,
            progress=False,
            threads=False,
        )
        normalized = normalize_ohlcv(data)
        return HistoryResult(
            ticker=ticker,
            data=normalized,
            provider="yfinance",
            note="Yahoo Finance via yfinance",
        )

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
        normalized = normalize_ohlcv(frame)
        return HistoryResult(
            ticker=ticker,
            data=normalized,
            provider="nasdaq",
            note="Nasdaq public historical endpoint fallback",
        )


def clean_ticker(ticker: str) -> str:
    cleaned = ticker.strip().upper()
    if not cleaned:
        raise ValueError("Ticker is required")
    return cleaned


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
