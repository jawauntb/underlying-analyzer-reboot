from __future__ import annotations

import copy
import json
import os
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

import requests

EXA_SEARCH_URL = "https://api.exa.ai/search"
EXA_CONTENTS_URL = "https://api.exa.ai/contents"

DEFAULT_REQUEST_INTERVAL_SECONDS = 5.0
DEFAULT_RESPONSE_CACHE_SECONDS = 6 * 60 * 60
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_BASE_SECONDS = 0.5
DEFAULT_BACKOFF_MAX_SECONDS = 8.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

__all__ = [
    "EXA_CONTENTS_URL",
    "EXA_SEARCH_URL",
    "ExaClient",
    "ExaError",
    "ExaResult",
    "build_research_pack",
]


class ExaError(RuntimeError):
    pass


@dataclass(frozen=True)
class CacheEntry:
    expires_at: float
    value: Any


@dataclass(frozen=True)
class ExaResult:
    title: str
    url: str
    published_date: str | None
    snippet: str
    text: str | None
    score: float | None
    author: str | None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ExaResult:
        if not isinstance(payload, Mapping):
            raise ExaError("Exa result payload was not a mapping")
        title = _string_or_empty(payload.get("title"))
        url = _string_or_empty(payload.get("url"))
        published_date = _optional_string(
            payload.get("publishedDate") or payload.get("published_date")
        )
        text = _optional_string(payload.get("text"))
        snippet_source = (
            payload.get("snippet")
            or payload.get("summary")
            or payload.get("highlight")
            or payload.get("highlights")
            or text
            or ""
        )
        snippet = _coerce_snippet(snippet_source)
        score = _optional_float(payload.get("score"))
        author = _optional_string(payload.get("author"))
        return cls(
            title=title,
            url=url,
            published_date=published_date,
            snippet=snippet,
            text=text,
            score=score,
            author=author,
        )


def _string_or_empty(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_snippet(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if item]
        return " … ".join(part for part in parts if part)
    if value is None:
        return ""
    return str(value).strip()


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class ExaRequestGate:
    def __init__(self) -> None:
        self._lock = Lock()
        self._last_request_at = 0.0

    def wait(self, interval_seconds: float) -> None:
        if interval_seconds <= 0:
            return
        with self._lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < interval_seconds:
                time.sleep(interval_seconds - elapsed)
            self._last_request_at = time.monotonic()


_EXA_REQUEST_GATE = ExaRequestGate()


class ExaClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        session: Any | None = None,
        timeout: int = 20,
        response_cache_seconds: float = DEFAULT_RESPONSE_CACHE_SECONDS,
        request_interval_seconds: float | None = None,
        max_retries: int | None = None,
        backoff_base_seconds: float | None = None,
        backoff_max_seconds: float | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("EXA_API_KEY")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.response_cache_seconds = response_cache_seconds
        self.request_interval_seconds = (
            request_interval_seconds
            if request_interval_seconds is not None
            else _float_env("EXA_REQUEST_INTERVAL_SECONDS", DEFAULT_REQUEST_INTERVAL_SECONDS)
        )
        self.max_retries = (
            max_retries
            if max_retries is not None
            else _int_env("EXA_MAX_RETRIES", DEFAULT_MAX_RETRIES)
        )
        self.backoff_base_seconds = (
            backoff_base_seconds
            if backoff_base_seconds is not None
            else _float_env("EXA_BACKOFF_BASE_SECONDS", DEFAULT_BACKOFF_BASE_SECONDS)
        )
        self.backoff_max_seconds = (
            backoff_max_seconds
            if backoff_max_seconds is not None
            else _float_env("EXA_BACKOFF_MAX_SECONDS", DEFAULT_BACKOFF_MAX_SECONDS)
        )
        self._cache: dict[str, CacheEntry] = {}
        self._cache_lock = Lock()

    def search(
        self,
        query: str,
        *,
        num_results: int = 8,
        start_published_date: str | None = None,
        end_published_date: str | None = None,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        type: str = "auto",
        use_autoprompt: bool = True,
        category: str | None = None,
    ) -> list[ExaResult]:
        if not query or not query.strip():
            return []
        body: dict[str, Any] = {
            "query": query.strip(),
            "numResults": int(num_results),
            "type": type,
            "useAutoprompt": bool(use_autoprompt),
        }
        if start_published_date:
            body["startPublishedDate"] = start_published_date
        if end_published_date:
            body["endPublishedDate"] = end_published_date
        if include_domains:
            body["includeDomains"] = list(include_domains)
        if exclude_domains:
            body["excludeDomains"] = list(exclude_domains)
        if category:
            body["category"] = category

        cache_key = _cache_key("search", body)
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        payload = self._post(EXA_SEARCH_URL, body)
        results = _parse_results(payload)
        self._remember(cache_key, results)
        return copy.deepcopy(results)

    def get_contents(
        self,
        urls: list[str],
        *,
        text_max_chars: int = 4000,
    ) -> list[ExaResult]:
        cleaned = [str(url).strip() for url in urls if isinstance(url, str) and url.strip()]
        if not cleaned:
            return []
        body: dict[str, Any] = {
            "ids": cleaned,
            "urls": cleaned,
            "text": {"maxCharacters": int(text_max_chars)},
        }
        cache_key = _cache_key("contents", body)
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        payload = self._post(EXA_CONTENTS_URL, body)
        results = _parse_results(payload)
        self._remember(cache_key, results)
        return copy.deepcopy(results)

    def search_with_contents(
        self,
        query: str,
        *,
        num_results: int = 6,
        text_max_chars: int = 3000,
        **kwargs: Any,
    ) -> list[ExaResult]:
        results = self.search(query, num_results=num_results, **kwargs)
        if not results:
            return results
        urls = [result.url for result in results if result.url]
        if not urls:
            return results
        try:
            contents = self.get_contents(urls, text_max_chars=text_max_chars)
        except ExaError:
            return results
        by_url = {item.url: item for item in contents if item.url}
        merged: list[ExaResult] = []
        for result in results:
            content = by_url.get(result.url)
            if content is None:
                merged.append(result)
                continue
            merged.append(
                ExaResult(
                    title=result.title or content.title,
                    url=result.url,
                    published_date=result.published_date or content.published_date,
                    snippet=result.snippet or content.snippet,
                    text=content.text or result.text,
                    score=result.score if result.score is not None else content.score,
                    author=result.author or content.author,
                )
            )
        return merged

    def _post(self, url: str, body: Mapping[str, Any]) -> Any:
        if not self.api_key:
            raise ExaError("Exa API key is not configured")
        attempts = max(0, self.max_retries) + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            self._throttle()
            try:
                response = self.session.post(
                    url,
                    json=dict(body),
                    headers=self._headers(),
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    self._backoff(None, attempt)
                    continue
                raise ExaError(f"Exa request failed: {exc}") from exc

            status = getattr(response, "status_code", 0)
            if status < 400:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ExaError(f"Exa response was not valid JSON: {exc}") from exc
            if status in RETRYABLE_STATUS_CODES and attempt + 1 < attempts:
                self._backoff(response, attempt)
                continue
            text_preview = ""
            response_text = getattr(response, "text", "")
            if isinstance(response_text, str):
                text_preview = response_text[:200]
            raise ExaError(
                f"Exa request failed with {status} for {url}: {text_preview}"
            )
        raise ExaError(f"Exa request failed for {url}: {last_error}")

    def _throttle(self) -> None:
        _EXA_REQUEST_GATE.wait(self.request_interval_seconds)

    def _backoff(self, response: Any | None, attempt: int) -> None:
        retry_after = _retry_after_seconds(response)
        if retry_after is None:
            retry_after = self.backoff_base_seconds * (2**attempt)
        delay = min(max(0.0, retry_after), self.backoff_max_seconds)
        if delay > 0:
            time.sleep(delay)

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": str(self.api_key or ""),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _cached(self, key: str) -> list[ExaResult] | None:
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if entry.expires_at < time.monotonic():
                self._cache.pop(key, None)
                return None
            return copy.deepcopy(entry.value)

    def _remember(self, key: str, value: list[ExaResult]) -> None:
        if self.response_cache_seconds <= 0:
            return
        with self._cache_lock:
            self._cache[key] = CacheEntry(
                expires_at=time.monotonic() + self.response_cache_seconds,
                value=copy.deepcopy(value),
            )


def _retry_after_seconds(response: Any | None) -> float | None:
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        return None
    value = headers.get("Retry-After")
    if not isinstance(value, str):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _cache_key(prefix: str, body: Mapping[str, Any]) -> str:
    return f"{prefix}:" + json.dumps(body, sort_keys=True, default=str)


def _parse_results(payload: Any) -> list[ExaResult]:
    if not isinstance(payload, Mapping):
        return []
    raw = payload.get("results")
    if not isinstance(raw, list):
        return []
    parsed: list[ExaResult] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        try:
            parsed.append(ExaResult.from_dict(item))
        except ExaError:
            continue
    return parsed


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


_CAPEX_QUERIES: dict[str, str] = {
    "semiconductors": "semiconductor capex AI accelerator foundry 2026 outlook hyperscaler spending",
    "networking": "data center capex AI infrastructure optical networking 2026 outlook",
    "data center": "data center capex AI infrastructure 2026 outlook hyperscaler buildout",
    "software": "enterprise software AI spending budget outlook 2026 cloud capex",
    "energy": "energy capex oil gas upstream 2026 outlook capital budget",
    "utilities": "utility capex grid transmission 2026 outlook data center load growth",
    "industrials": "industrial capex automation manufacturing 2026 outlook capital spending",
    "biotech": "biotech R&D spending pipeline 2026 outlook FDA approvals",
    "consumer": "consumer discretionary capex 2026 outlook retail spending",
    "financials": "bank capital allocation 2026 outlook loan growth credit",
}


def _capex_query_for(industry: str | None, sector: str | None, company_name: str) -> str:
    for label in (industry, sector):
        if not isinstance(label, str):
            continue
        lowered = label.lower()
        for keyword, query in _CAPEX_QUERIES.items():
            if keyword in lowered:
                return query
    return (
        f"{company_name} industry capex AI infrastructure 2026 outlook capital spending"
    )


def _industry_language_phrase(industry: str | None, sector: str | None) -> str:
    pieces: list[str] = []
    for label in (industry, sector):
        if isinstance(label, str) and label.strip():
            pieces.append(label.strip())
    return " ".join(pieces)


def _curated_queries(
    ticker: str,
    company_name: str,
    *,
    industry: str | None,
    sector: str | None,
) -> dict[str, dict[str, Any]]:
    industry_phrase = _industry_language_phrase(industry, sector)
    language_query = (
        f"{company_name} AI data center optical 800G 1.6T hyperscale"
        + (f" {industry_phrase}" if industry_phrase else "")
    ).strip()
    return {
        "recent_news_90d": {
            "query": f"{company_name} {ticker} earnings news guidance",
            "start_published_date": _iso_days_ago(90),
            "num_results": 8,
        },
        "product_and_customer": {
            "query": (
                f"{company_name} product launch customer deal contract win"
            ),
            "start_published_date": _iso_days_ago(180),
            "num_results": 8,
        },
        "language_mutation": {
            "query": language_query,
            "start_published_date": _iso_days_ago(365),
            "num_results": 8,
        },
        "peer_and_reclassification": {
            "query": f"{company_name} peers competitors vs sector comparison",
            "start_published_date": _iso_days_ago(365),
            "num_results": 6,
        },
        "sell_side_framing": {
            "query": (
                f"{company_name} {ticker} analyst price target upgrade downgrade"
            ),
            "start_published_date": _iso_days_ago(90),
            "num_results": 6,
        },
        "capex_cycle_context": {
            "query": _capex_query_for(industry, sector, company_name),
            "start_published_date": _iso_days_ago(365),
            "num_results": 6,
        },
    }


def build_research_pack(
    client: ExaClient | None,
    ticker: str,
    company_name: str,
    *,
    industry: str | None = None,
    sector: str | None = None,
) -> dict[str, Any]:
    symbol = (ticker or "").strip().upper()
    name = (company_name or "").strip() or symbol
    queries = _curated_queries(symbol, name, industry=industry, sector=sector)
    empty_buckets = {bucket: [] for bucket in queries}

    if client is None or not getattr(client, "api_key", None):
        return {
            "Status": "not configured",
            "Provider": "Exa",
            "Ticker": symbol,
            "Company": name,
            "Queries": empty_buckets,
            "Citations": [],
            "Errors": ["EXA_API_KEY is not configured"],
        }

    bucket_results: dict[str, list[dict[str, Any]]] = {bucket: [] for bucket in queries}
    citations: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_urls: set[str] = set()
    any_success = False

    for bucket, params in queries.items():
        query = str(params.get("query") or "").strip()
        if not query:
            continue
        try:
            results = client.search(
                query,
                num_results=int(params.get("num_results", 6)),
                start_published_date=params.get("start_published_date"),
                end_published_date=params.get("end_published_date"),
            )
        except ExaError as exc:
            errors.append(f"{bucket}: {exc}")
            continue

        any_success = True
        bucket_payload: list[dict[str, Any]] = []
        for result in results:
            bucket_payload.append(asdict(result))
            if result.url and result.url not in seen_urls:
                seen_urls.add(result.url)
                citations.append(
                    {
                        "url": result.url,
                        "title": result.title,
                        "published_date": result.published_date,
                        "query_bucket": bucket,
                    }
                )
        bucket_results[bucket] = bucket_payload

    status = "available" if any_success else "unavailable"
    return {
        "Status": status,
        "Provider": "Exa",
        "Ticker": symbol,
        "Company": name,
        "Queries": bucket_results,
        "Citations": citations,
        "Errors": errors,
    }
