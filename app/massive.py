"""Massive REST adapter.

The adapter deliberately uses the documented REST surface instead of an SDK so
request construction, retries, pagination, and subscription failures remain
observable and fixture-testable.
"""

from __future__ import annotations

import base64
import binascii
import os
import time
from collections.abc import Callable
from datetime import date
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import pandas as pd
import requests

from app.market_data import (
    HistoryResult,
    MarketDataCapabilityError,
    MarketDataError,
    OptionChainResult,
    choose_expiry,
    epoch_to_naive_utc,
    massive_interval_spec,
    normalize_history_interval,
    normalize_ohlcv,
)


class MassiveProviderError(MarketDataError):
    """A response or transport failure from Massive."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MassiveSession(Protocol):
    def get(self, url: str, *, params: dict[str, Any], timeout: float) -> Any: ...


class MassiveProvider:
    name = "massive"
    note = "Massive market data; freshness depends on the configured Stocks and Options plans"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.massive.com",
        session: MassiveSession | None = None,
        timeout: float = 15.0,
        max_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
        max_pages: int = 20,
    ) -> None:
        if not api_key.strip():
            raise ValueError("MASSIVE_API_KEY is required")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.sleep = sleep
        self.max_pages = max(1, max_pages)

    @classmethod
    def from_env(cls) -> MassiveProvider | _MissingMassiveProvider:
        key = os.getenv("MASSIVE_API_KEY", "")
        if not key:
            return _MissingMassiveProvider()
        try:
            timeout = float(os.getenv("MASSIVE_TIMEOUT_SECONDS", "15"))
        except ValueError:
            timeout = 15.0
        return cls(
            key,
            base_url=os.getenv("MASSIVE_REST_BASE_URL", "https://api.massive.com"),
            timeout=max(1.0, timeout),
            max_retries=_int_env("MASSIVE_MAX_RETRIES", 2, minimum=0, maximum=5),
            max_pages=_int_env("MASSIVE_MAX_PAGES", 20, minimum=1, maximum=100),
        )

    def _request_json(
        self,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = (
            path_or_url
            if path_or_url.startswith("http")
            else f"{self.base_url}/{path_or_url.lstrip('/')}"
        )
        query = self._bounded_params(params)
        query["apiKey"] = self.api_key
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(url, params=query, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise MassiveProviderError("Massive request failed") from exc
                self.sleep(self._backoff(attempt))
                continue

            status_code = getattr(response, "status_code", None)
            if (
                status_code == 429 or (isinstance(status_code, int) and status_code >= 500)
            ) and attempt < self.max_retries:
                self.sleep(self._retry_delay(response, attempt))
                continue
            if isinstance(status_code, int) and status_code in {401, 403}:
                raise MassiveProviderError(
                    "Massive authentication or subscription rejected the request",
                    status_code=status_code,
                )
            try:
                response.raise_for_status()
            except requests.RequestException as exc:
                raise MassiveProviderError(
                    f"Massive request failed with status {status_code}", status_code=status_code
                ) from exc
            try:
                payload = response.json()
            except (TypeError, ValueError) as exc:
                raise MassiveProviderError(
                    "Massive returned invalid JSON", status_code=status_code
                ) from exc
            if not isinstance(payload, dict):
                raise MassiveProviderError(
                    "Massive returned a non-object response", status_code=status_code
                )
            if str(payload.get("status", "OK")).upper() == "ERROR":
                raise MassiveProviderError(
                    "Massive provider rejected the request", status_code=status_code
                )
            return payload
        raise MassiveProviderError("Massive request exhausted retries")

    def _bounded_params(self, params: dict[str, Any] | None) -> dict[str, Any]:
        query = {key: value for key, value in (params or {}).items() if key != "apiKey"}
        if "limit" in query:
            try:
                query["limit"] = max(1, min(1000, int(query["limit"])))
            except (TypeError, ValueError):
                query["limit"] = 1000
        return query

    def _paginate(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = dict(params or {})
        cursor = query.pop("cursor", None)
        first_path = path
        if cursor:
            try:
                padded = str(cursor) + "=" * (-len(str(cursor)) % 4)
                first_path = base64.urlsafe_b64decode(padded).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
                raise MassiveProviderError("Massive cursor is invalid") from exc
            self._validate_next_url(first_path)
        first = self._request_json(first_path, params=query)
        results = list(first.get("results") or []) if isinstance(first.get("results"), list) else []
        next_url = first.get("next_url")
        seen = {path, first_path}
        pages = 1
        while isinstance(next_url, str) and next_url and pages < self.max_pages:
            if next_url in seen:
                raise MassiveProviderError("Massive pagination repeated a next_url")
            self._validate_next_url(next_url)
            seen.add(next_url)
            page = self._request_json(next_url)
            page_results = page.get("results")
            if isinstance(page_results, list):
                results.extend(page_results)
            next_url = page.get("next_url")
            pages += 1
        payload = dict(first)
        payload["results"] = results
        if isinstance(next_url, str) and next_url:
            parsed = urlparse(next_url)
            safe_query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "apiKey"]
            safe_url = urlunparse(parsed._replace(query=urlencode(safe_query)))
            payload["next_cursor"] = (
                base64.urlsafe_b64encode(safe_url.encode()).decode().rstrip("=")
            )
        payload.pop("next_url", None)
        return payload

    def _validate_next_url(self, next_url: str) -> None:
        if not next_url.startswith("http"):
            return
        parsed = urlparse(next_url)
        base = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or parsed.netloc != base.netloc:
            raise MassiveProviderError("Massive pagination returned an untrusted next_url")

    def _backoff(self, attempt: int) -> float:
        return min(8.0, 0.5 * (2**attempt))

    def _retry_delay(self, response: Any, attempt: int) -> float:
        headers = getattr(response, "headers", {}) or {}
        retry_after = headers.get("Retry-After") if hasattr(headers, "get") else None
        if not isinstance(retry_after, int | float | str):
            return self._backoff(attempt)
        try:
            return max(0.0, min(2.0, float(retry_after)))
        except (TypeError, ValueError):
            return self._backoff(attempt)

    def get_history(self, ticker: str, *, start: date, end: date, interval: str) -> HistoryResult:
        resolved = normalize_history_interval(interval)
        multiplier, timespan = massive_interval_spec(resolved)
        payload = self._paginate(
            f"/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{start.isoformat()}/{end.isoformat()}",
            params={"adjusted": "true", "sort": "asc", "limit": 50000},
        )
        rows = payload.get("results")
        if not isinstance(rows, list):
            raise MassiveProviderError("Massive historical response is missing results")
        frame = pd.DataFrame(
            [
                {
                    "Date": epoch_to_naive_utc(row.get("t")),
                    "Open": row.get("o"),
                    "High": row.get("h"),
                    "Low": row.get("l"),
                    "Close": row.get("c"),
                    "Volume": row.get("v", 0),
                }
                for row in rows
                if isinstance(row, dict) and row.get("t") is not None
            ]
        )
        if frame.empty:
            return HistoryResult(ticker, frame, self.name, self.note, interval=resolved)
        frame = frame.set_index("Date")
        frame["Adj Close"] = frame["Close"]
        return HistoryResult(
            ticker, normalize_ohlcv(frame), self.name, self.note, interval=resolved
        )

    def get_profile(self, ticker: str) -> dict[str, Any]:
        payload = self._request_json(f"/v3/reference/tickers/{ticker}")
        result = payload.get("results")
        if not isinstance(result, dict):
            raise MassiveProviderError(f"Massive returned no profile for {ticker}")
        return {
            "longName": result.get("name"),
            "shortName": result.get("name"),
            "symbol": result.get("ticker", ticker),
            "exchange": result.get("primary_exchange"),
            "market": result.get("market"),
            "quoteType": result.get("type"),
            "currency": result.get("currency_name"),
            "marketCap": result.get("market_cap"),
            "industry": result.get("sic_description") or result.get("industry"),
            "longBusinessSummary": result.get("description"),
            "website": result.get("homepage_url"),
            "country": result.get("locale"),
            "fullTimeEmployees": result.get("total_employees"),
            "cik": result.get("cik"),
            "composite_figi": result.get("composite_figi"),
            "share_class_figi": result.get("share_class_figi"),
            "active": result.get("active"),
            "last_updated_utc": result.get("last_updated_utc"),
            "massive": result,
        }

    def search_securities(self, query: str, *, limit: int) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        seen_symbols: set[str] = set()
        for market in ("stocks", "indices", "crypto"):
            payload = self._request_json(
                "/v3/reference/tickers",
                params={
                    "market": market,
                    "active": "true",
                    "search": query,
                    "order": "asc",
                    "sort": "ticker",
                    "limit": limit,
                },
            )
            raw_results = payload.get("results")
            for quote in raw_results if isinstance(raw_results, list) else []:
                if not isinstance(quote, dict):
                    continue
                symbol = str(quote.get("ticker", "")).strip().upper()
                if not symbol or symbol in seen_symbols:
                    continue
                seen_symbols.add(symbol)
                quote_type = str(quote.get("type") or "").upper()
                asset_type = (
                    "crypto"
                    if market == "crypto" or quote_type == "CRYPTOCURRENCY"
                    else "index"
                    if market == "indices" or quote_type == "INDEX"
                    else "etf"
                    if quote_type == "ETF"
                    else "equity"
                )
                results.append(
                    {
                        "symbol": symbol,
                        "name": str(quote.get("name") or "").strip(),
                        "exchange": str(quote.get("primary_exchange") or "").strip(),
                        "asset_type": asset_type,
                    }
                )
        return results[:limit]

    def get_expirations(self, ticker: str) -> list[str]:
        payload = self._paginate(
            "/v3/reference/options/contracts",
            params={
                "underlying_ticker": ticker,
                "limit": 1000,
                "order": "asc",
                "sort": "expiration_date",
            },
        )
        expirations = {
            str(row["expiration_date"])
            for row in payload.get("results", [])
            if isinstance(row, dict) and row.get("expiration_date")
        }
        return sorted(expirations)

    def get_option_chain(self, ticker: str, expiry: str | None = None) -> OptionChainResult:
        expirations = self.get_expirations(ticker)
        selected_expiry = choose_expiry(expirations, expiry) if expirations else expiry
        params: dict[str, Any] = {"limit": 250, "order": "asc", "sort": "strike_price"}
        if selected_expiry:
            params["expiration_date"] = selected_expiry
        payload = self._paginate(f"/v3/snapshot/options/{ticker}", params=params)
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise MassiveProviderError(f"Massive returned no options chain for {ticker}")
        current_price = self._underlying_price(raw_results)
        rows = self._option_rows(raw_results, current_price)
        if not rows or selected_expiry is None:
            raise MarketDataError(
                f"No usable option rows for {ticker} {selected_expiry or 'nearest expiry'}"
            )
        return OptionChainResult(
            ticker, selected_expiry, current_price, rows, expirations, self.name, self.note
        )

    def _underlying_price(self, results: list[Any]) -> float:
        for result in results:
            if not isinstance(result, dict):
                continue
            underlying = result.get("underlying_asset")
            if isinstance(underlying, dict):
                for key in ("price", "value"):
                    if isinstance(underlying.get(key), int | float):
                        return float(underlying[key])
        raise MarketDataError("Massive options response did not include an underlying price")

    def _option_rows(self, results: list[Any], current_price: float) -> list[dict[str, Any]]:
        by_strike: dict[float, dict[str, Any]] = {}
        for result in results:
            if not isinstance(result, dict):
                continue
            raw_details = result.get("details")
            details: dict[str, Any] = raw_details if isinstance(raw_details, dict) else result
            strike = details.get("strike_price")
            contract_type = str(details.get("contract_type") or "").lower()
            if not isinstance(strike, int | float) or contract_type not in {"call", "put"}:
                continue
            row = by_strike.setdefault(float(strike), {"strike": float(strike)})
            prefix = "call" if contract_type == "call" else "put"
            row[f"{prefix}_open_interest"] = float(result.get("open_interest") or 0)
            row[f"{prefix}_contract"] = details.get("ticker") or result.get("ticker")
            row[f"{prefix}_implied_volatility"] = result.get("implied_volatility")
            raw_greeks = result.get("greeks")
            if isinstance(raw_greeks, dict):
                for greek in ("delta", "gamma", "theta", "vega"):
                    row[f"{prefix}_{greek}"] = raw_greeks.get(greek)
            raw_trade = result.get("last_trade")
            trade: dict[str, Any] = raw_trade if isinstance(raw_trade, dict) else {}
            raw_quote = result.get("last_quote")
            quote: dict[str, Any] = raw_quote if isinstance(raw_quote, dict) else {}
            raw_day = result.get("day")
            day: dict[str, Any] = raw_day if isinstance(raw_day, dict) else {}
            row[f"{prefix}_last"] = float(
                trade.get("price") or trade.get("p") or day.get("close") or day.get("c") or 0
            )
            row[f"{prefix}_bid"] = quote.get("bid_price") or quote.get("bp")
            row[f"{prefix}_ask"] = quote.get("ask_price") or quote.get("ap")
            row[f"{prefix}_volume"] = day.get("volume") or day.get("v") or 0
        nearby = sorted(by_strike, key=lambda strike: abs(strike - current_price))[:9]
        rows: list[dict[str, Any]] = []
        for strike in sorted(nearby):
            row = by_strike[strike]
            call_oi = float(row.get("call_open_interest", 0))
            put_oi = float(row.get("put_open_interest", 0))
            rows.append(
                {
                    "strike": strike,
                    "call_open_interest": call_oi,
                    "put_open_interest": put_oi,
                    "call_last": float(row.get("call_last", 0)),
                    "put_last": float(row.get("put_last", 0)),
                    **{
                        key: value
                        for key, value in row.items()
                        if key not in {
                            "strike",
                            "call_open_interest",
                            "put_open_interest",
                            "call_last",
                            "put_last",
                        }
                    },
                    "net_open_interest": call_oi - put_oi,
                    "put_call_ratio": put_oi / call_oi if call_oi else 0.0,
                }
            )
        return rows

    def get_snapshot(self, ticker: str, *, asset_class: str = "stocks") -> dict[str, Any]:
        if asset_class != "stocks":
            raise MarketDataCapabilityError(
                f"Massive snapshot adapter does not support {asset_class!r}"
            )
        return self._request_json(f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}")

    def get_aggregates(
        self,
        ticker: str,
        *,
        multiplier: int,
        timespan: str,
        start: str,
        end: str,
        asset_class: str = "stocks",
    ) -> dict[str, Any]:
        if asset_class not in {"stocks", "options"}:
            raise MarketDataCapabilityError(f"Massive aggregates do not support {asset_class!r}")
        return self._paginate(
            f"/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{start}/{end}",
            params={"adjusted": "true", "sort": "asc", "limit": 50000},
        )

    def get_trades(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._paginate(f"/v3/trades/{ticker}", params=params)

    def get_quotes(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._paginate(f"/v3/quotes/{ticker}", params=params)

    def get_contracts(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = {key: value for key, value in (params or {}).items() if key != "underlying_ticker"}
        query["underlying_ticker"] = ticker
        query.setdefault("limit", 1000)
        return self._paginate("/v3/reference/options/contracts", params=query)

    def get_events(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._paginate(f"/vX/reference/tickers/{ticker}/events", params=params)

    def get_dividends(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = {key: value for key, value in (params or {}).items() if key != "ticker"}
        query["ticker"] = ticker
        query.setdefault("limit", 100)
        return self._paginate("/stocks/v1/dividends", params=query)

    def get_splits(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = {key: value for key, value in (params or {}).items() if key != "ticker"}
        query["ticker"] = ticker
        query.setdefault("limit", 100)
        return self._paginate("/stocks/v1/splits", params=query)

    def get_financials(
        self,
        ticker: str,
        *,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        paths = {
            "income": "/stocks/financials/v1/income-statements",
            "income-statements": "/stocks/financials/v1/income-statements",
            "balance": "/stocks/financials/v1/balance-sheets",
            "balance-sheets": "/stocks/financials/v1/balance-sheets",
            "cash-flow": "/stocks/financials/v1/cash-flow-statements",
            "cash-flow-statements": "/stocks/financials/v1/cash-flow-statements",
            "ratios": "/stocks/financials/v1/ratios",
        }
        path = paths.get(statement.strip().lower())
        if path is None:
            raise MarketDataCapabilityError(
                "Massive financial statement must be income, balance, cash-flow, or ratios"
            )
        query = {key: value for key, value in (params or {}).items() if key != "tickers"}
        query["tickers"] = ticker
        return self._paginate(path, params=query)

    def get_market_status(self) -> dict[str, Any]:
        return self._request_json("/v1/marketstatus/now")

    def get_news(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = {key: value for key, value in (params or {}).items() if key != "ticker"}
        query.update({"ticker": ticker})
        query.setdefault("order", "desc")
        query.setdefault("sort", "published_utc")
        query.setdefault("limit", 20)
        return self._paginate("/v2/reference/news", params=query)

    def get_corporate_events(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = dict(params or {})
        query.setdefault("limit", 100)
        return self._paginate("/tmx/v1/corporate-events", params=query)

    def get_ipos(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = dict(params or {})
        query.setdefault("limit", 100)
        return self._paginate("/vX/reference/ipos", params=query)

    def get_conditions(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = dict(params or {})
        query.setdefault("limit", 1000)
        return self._paginate("/v3/reference/conditions", params=query)

    def get_all_snapshot(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request_json(
            "/v2/snapshot/locale/us/markets/stocks/tickers", params=params
        )

    def get_option_snapshot(self, underlying: str, contract: str) -> dict[str, Any]:
        underlying = underlying.strip().upper()
        contract = contract.strip().upper()
        if not contract.startswith("O:"):
            contract = f"O:{contract}"
        return self._request_json(f"/v3/snapshot/options/{underlying}/{contract}")


class _MissingMassiveProvider:
    name = "massive"
    note = "Massive is not configured; legacy fallback routing is active"

    def _missing(self, *_: Any, **__: Any) -> None:
        raise MarketDataCapabilityError("MASSIVE_API_KEY is not configured")

    get_history = _missing
    get_profile = _missing
    search_securities = _missing
    get_option_chain = _missing
    get_expirations = _missing
    get_snapshot = _missing
    get_aggregates = _missing
    get_trades = _missing
    get_quotes = _missing
    get_contracts = _missing
    get_events = _missing
    get_dividends = _missing
    get_splits = _missing
    get_financials = _missing
    get_market_status = _missing
    get_news = _missing
    get_corporate_events = _missing
    get_ipos = _missing
    get_conditions = _missing
    get_all_snapshot = _missing
    get_option_snapshot = _missing


def _int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(os.getenv(name, str(default)))))
    except ValueError:
        return default
