from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

from app._perf import TTLCache, tune_session

# TradingView watchlist symbol lists change rarely; a short TTL dedupes refetches of
# the same public watchlist within a scheduled batch (multiple rules can share a URL)
# without serving meaningfully stale symbols.
WATCHLIST_CACHE_TTL_SECONDS = 60.0


class WatchlistError(ValueError):
    """Raised when a watchlist URL cannot be resolved into symbols."""


@dataclass(frozen=True)
class WatchlistSymbol:
    raw: str
    exchange: str | None
    symbol: str
    ticker: str


@dataclass(frozen=True)
class WatchlistResult:
    id: int | None
    name: str
    source_url: str
    symbols: list[WatchlistSymbol]

    @property
    def tickers(self) -> list[str]:
        return [symbol.ticker for symbol in self.symbols]


class TradingViewWatchlistClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        cache_ttl_seconds: float = WATCHLIST_CACHE_TTL_SECONDS,
    ) -> None:
        self.session = session or requests.Session()
        # Widen the connection pool for concurrent watchlist fetches / keep-alive reuse.
        tune_session(self.session, pool_maxsize=32)
        self._cache = TTLCache(cache_ttl_seconds)

    def get_watchlist(self, url: str) -> WatchlistResult:
        watchlist_url = normalize_watchlist_url(url)
        cached = self._cache.get(watchlist_url)
        if cached is not None:
            return cached
        response = self.session.get(
            watchlist_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
                ),
            },
            timeout=15,
        )
        response.raise_for_status()
        result = parse_tradingview_watchlist(response.text, source_url=watchlist_url)
        self._cache.set(watchlist_url, result)
        return result


def normalize_watchlist_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise WatchlistError("TradingView watchlist URL must start with http or https")
    if parsed.netloc.lower() not in {"tradingview.com", "www.tradingview.com"}:
        raise WatchlistError("Only public tradingview.com watchlist URLs are supported")

    match = re.search(r"/watchlists/(\d+)/?", parsed.path)
    if not match:
        raise WatchlistError("TradingView watchlist URL must look like /watchlists/{id}/")

    return f"https://www.tradingview.com/watchlists/{match.group(1)}/"


def parse_tradingview_watchlist(html_text: str, *, source_url: str) -> WatchlistResult:
    for payload in iter_init_data_payloads(html_text):
        shared_watchlist = payload.get("sharedWatchlist")
        if not isinstance(shared_watchlist, dict):
            continue

        watchlist = shared_watchlist.get("list")
        if not isinstance(watchlist, dict):
            continue

        raw_symbols = watchlist.get("symbols")
        if not isinstance(raw_symbols, list):
            continue

        symbols = unique_symbols(
            normalize_tradingview_symbol(str(symbol))
            for symbol in raw_symbols
            if str(symbol).strip()
        )
        if not symbols:
            raise WatchlistError("TradingView watchlist did not include usable symbols")

        return WatchlistResult(
            id=coerce_int(watchlist.get("id")),
            name=str(watchlist.get("name") or "TradingView watchlist"),
            source_url=source_url,
            symbols=symbols,
        )

    raise WatchlistError("Could not find public TradingView watchlist data on that page")


def iter_init_data_payloads(html_text: str) -> list[dict[str, Any]]:
    matches = re.findall(
        r'<script[^>]+type=["\']application/prs\.init-data\+json["\'][^>]*>(.*?)</script>',
        html_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    payloads: list[dict[str, Any]] = []
    for match in matches:
        try:
            data = json.loads(html.unescape(match))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            payloads.append(data)
    return payloads


def normalize_tradingview_symbol(raw_symbol: str) -> WatchlistSymbol:
    raw = raw_symbol.strip().upper()
    if not raw:
        raise WatchlistError("TradingView symbol is empty")

    exchange: str | None = None
    symbol = raw
    if ":" in raw:
        exchange, symbol = raw.split(":", 1)

    ticker = symbol.replace(".", "-")
    suffix = {
        "ASX": ".AX",
        "TSX": ".TO",
        "TSXV": ".V",
        "LSE": ".L",
        "FWB": ".F",
        "XETR": ".DE",
        "HKEX": ".HK",
        "TSE": ".T",
    }.get(exchange or "")
    if suffix:
        ticker = f"{ticker}{suffix}"

    return WatchlistSymbol(raw=raw, exchange=exchange, symbol=symbol, ticker=ticker)


def unique_symbols(symbols: Any) -> list[WatchlistSymbol]:
    unique: list[WatchlistSymbol] = []
    seen: set[str] = set()
    for symbol in symbols:
        if symbol.ticker in seen:
            continue
        seen.add(symbol.ticker)
        unique.append(symbol)
    return unique


def coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def watchlist_payload(watchlist: WatchlistResult | None) -> dict[str, Any] | None:
    if watchlist is None:
        return None
    return {
        "id": watchlist.id,
        "name": watchlist.name,
        "source_url": watchlist.source_url,
        "tickers": watchlist.tickers,
        "symbols": [
            {
                "raw": symbol.raw,
                "exchange": symbol.exchange,
                "symbol": symbol.symbol,
                "ticker": symbol.ticker,
            }
            for symbol in watchlist.symbols
        ],
    }
