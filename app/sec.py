from __future__ import annotations

import copy
import html
import os
import re
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from typing import Any

import requests
import yfinance as yf

from app._perf import tune_session
from app.analysis import trim_text
from app.market_data import clean_ticker

SEC_DATA_BASE = "https://data.sec.gov"
SEC_WEB_BASE = "https://www.sec.gov"
DEFAULT_SEC_USER_AGENT = (
    "The Underlying Analyzer Reboot research app contact:jawauntb@users.noreply.github.com"
)
DEFAULT_REQUEST_INTERVAL_SECONDS = 0.35
DEFAULT_RESPONSE_CACHE_SECONDS = 24 * 60 * 60
DEFAULT_SOURCE_PACK_CACHE_SECONDS = 6 * 60 * 60
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_BASE_SECONDS = 0.5
DEFAULT_BACKOFF_MAX_SECONDS = 8.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

SECTION_SPECS = {
    "Business": {
        "item": "Item 1",
        "heading": "Business",
        "starts": [r"\bItem\s+1\.?\s+Business\b"],
        "ends": [
            r"\bItem\s+1A\.?\s+Risk\s+Factors\b",
            r"\bItem\s+1B\.?\s+Unresolved",
            r"\bItem\s+2\.?\s+Properties\b",
        ],
    },
    "Risk Factors": {
        "item": "Item 1A",
        "heading": "Risk Factors",
        "starts": [r"\bItem\s+1A\.?\s+Risk\s+Factors\b"],
        "ends": [
            r"\bItem\s+1B\.?\s+Unresolved",
            r"\bItem\s+2\.?\s+Properties\b",
            r"\bItem\s+3\.?\s+Legal",
        ],
    },
    "MD&A": {
        "item": "Item 7",
        "heading": "Management's Discussion And Analysis",
        "starts": [
            r"\bItem\s+7\.?\s+Management[’']?s\s+Discussion\s+and\s+Analysis\b"
        ],
        "ends": [
            r"\bItem\s+7A\.?\s+Quantitative",
            r"\bItem\s+8\.?\s+Financial",
            r"\bItem\s+9\.?\s+Changes",
        ],
    },
}

EARNINGS_SECTION_SPECS = {
    "Earnings Release": {
        "item": "Item 2.02",
        "heading": "Results Of Operations And Financial Condition",
        "starts": [
            r"\bItem\s+2\.02\.?\s+Results\s+of\s+Operations\s+and\s+Financial\s+Condition\b"
        ],
        "ends": [
            r"\bItem\s+2\.03\b",
            r"\bItem\s+7\.01\b",
            r"\bItem\s+8\.01\b",
            r"\bItem\s+9\.01\b",
            r"\bSIGNATURES?\b",
        ],
    },
    "Event Update": {
        "item": "Item 7.01",
        "heading": "Regulation FD Disclosure",
        "starts": [r"\bItem\s+7\.01\.?\s+Regulation\s+FD\s+Disclosure\b"],
        "ends": [
            r"\bItem\s+8\.01\b",
            r"\bItem\s+9\.01\b",
            r"\bSIGNATURES?\b",
        ],
    },
}

FACT_SPECS = [
    (
        "Revenue",
        "us-gaap",
        ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"),
        "USD",
    ),
    ("Operating Income", "us-gaap", ("OperatingIncomeLoss",), "USD"),
    ("Net Income", "us-gaap", ("NetIncomeLoss",), "USD"),
    ("Assets", "us-gaap", ("Assets",), "USD"),
    ("Liabilities", "us-gaap", ("Liabilities",), "USD"),
    ("Stockholders Equity", "us-gaap", ("StockholdersEquity",), "USD"),
    (
        "Cash And Equivalents",
        "us-gaap",
        (
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        ),
        "USD",
    ),
    (
        "Long Term Debt",
        "us-gaap",
        ("LongTermDebt", "LongTermDebtAndFinanceLeaseObligations"),
        "USD",
    ),
    ("Diluted EPS", "us-gaap", ("EarningsPerShareDiluted",), None),
    ("Shares Outstanding", "dei", ("EntityCommonStockSharesOutstanding",), "shares"),
]


class SecDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class CacheEntry:
    expires_at: float
    value: Any


class SecRequestGate:
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


_SEC_REQUEST_GATE = SecRequestGate()


def float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class SecClient:
    def __init__(
        self,
        *,
        session: Any | None = None,
        user_agent: str | None = None,
        timeout: int = 20,
        request_interval_seconds: float | None = None,
        response_cache_seconds: float | None = None,
        source_pack_cache_seconds: float | None = None,
        max_retries: int | None = None,
        backoff_base_seconds: float | None = None,
        backoff_max_seconds: float | None = None,
    ) -> None:
        self.session = session or requests.Session()
        # Widen the connection pool so parallel per-filing fetches reuse keep-alive
        # sockets instead of churning new ones. Only real requests.Session objects
        # expose ``mount``; test doubles are left untouched.
        if isinstance(self.session, requests.Session):
            tune_session(self.session, pool_maxsize=32)
        self.user_agent = user_agent or os.getenv("SEC_USER_AGENT") or DEFAULT_SEC_USER_AGENT
        self.timeout = timeout
        self.request_interval_seconds = (
            request_interval_seconds
            if request_interval_seconds is not None
            else float_env("SEC_REQUEST_INTERVAL_SECONDS", DEFAULT_REQUEST_INTERVAL_SECONDS)
        )
        self.response_cache_seconds = (
            response_cache_seconds
            if response_cache_seconds is not None
            else float_env("SEC_RESPONSE_CACHE_SECONDS", DEFAULT_RESPONSE_CACHE_SECONDS)
        )
        self.source_pack_cache_seconds = (
            source_pack_cache_seconds
            if source_pack_cache_seconds is not None
            else float_env("SEC_SOURCE_PACK_CACHE_SECONDS", DEFAULT_SOURCE_PACK_CACHE_SECONDS)
        )
        self.max_retries = (
            max_retries
            if max_retries is not None
            else int_env("SEC_MAX_RETRIES", DEFAULT_MAX_RETRIES)
        )
        self.backoff_base_seconds = (
            backoff_base_seconds
            if backoff_base_seconds is not None
            else float_env("SEC_BACKOFF_BASE_SECONDS", DEFAULT_BACKOFF_BASE_SECONDS)
        )
        self.backoff_max_seconds = (
            backoff_max_seconds
            if backoff_max_seconds is not None
            else float_env("SEC_BACKOFF_MAX_SECONDS", DEFAULT_BACKOFF_MAX_SECONDS)
        )
        self._ticker_map: dict[str, dict[str, Any]] | None = None
        self._json_cache: dict[str, CacheEntry] = {}
        self._text_cache: dict[str, CacheEntry] = {}
        self._source_pack_cache: dict[str, CacheEntry] = {}
        self._cache_lock = Lock()
        self._source_pack_lock = Lock()

    def get_source_pack(self, ticker: str) -> dict[str, Any]:
        symbol = clean_ticker(ticker)
        cached = self.cached_value(self._source_pack_cache, symbol)
        if cached is not None:
            return cached

        with self._source_pack_lock:
            cached = self.cached_value(self._source_pack_cache, symbol)
            if cached is not None:
                return cached

            try:
                pack = self.edgar_source_pack(symbol)
            except SecDataError as exc:
                fallback = self.yahoo_source_pack(symbol, reason=str(exc))
                if fallback["Status"] == "unavailable":
                    raise
                pack = fallback

            self.remember_value(
                self._source_pack_cache,
                symbol,
                pack,
                ttl_seconds=self.source_pack_cache_seconds,
            )
            return copy.deepcopy(pack)

    def edgar_source_pack(self, symbol: str) -> dict[str, Any]:
        cik = self.cik_for_ticker(symbol)
        submissions = self.submissions(cik)
        filings = latest_filings(submissions)
        # The 10-K/10-Q text, 8-K text and XBRL company facts come from three
        # independent, idempotent SEC endpoints; fetch them concurrently. The
        # shared request gate still rate-limits actual dispatch, so this mainly
        # overlaps document parsing with the next request's network wait.
        with ThreadPoolExecutor(max_workers=3) as executor:
            sections_future = executor.submit(self.filing_sections, filings)
            earnings_future = executor.submit(self.earnings_sections, filings)
            facts_future = executor.submit(self.company_facts, cik)
            # Resolve in the original sequential order so an escaping SecDataError
            # from filing_sections still wins and triggers the Yahoo fallback.
            sections, section_errors = sections_future.result()
            earnings_sections, earnings_errors = earnings_future.result()
            facts, fact_errors = facts_future.result()
        errors = section_errors + earnings_errors + fact_errors
        citations = source_citations(filings, sections, facts, earnings_sections)
        status = (
            "available"
            if sections or facts or earnings_sections
            else "partial"
            if filings
            else "unavailable"
        )
        return {
            "Status": status,
            "Provider": "SEC EDGAR",
            "Ticker": symbol,
            "CIK": cik,
            "Company Name": submissions.get("name"),
            "SIC": submissions.get("sic"),
            "SIC Description": submissions.get("sicDescription"),
            "Exchanges": submissions.get("exchanges") or [],
            "Filings": filings,
            "Filing Sections": sections,
            "Earnings Sections": earnings_sections,
            "Company Facts": facts,
            "Citations": citations,
            "Errors": errors,
        }

    def yahoo_source_pack(self, symbol: str, *, reason: str) -> dict[str, Any]:
        try:
            filings = yf.Ticker(symbol).get_sec_filings()
        except Exception as exc:
            return {
                "Status": "unavailable",
                "Provider": "Yahoo Finance SEC filings mirror",
                "Ticker": symbol,
                "Filings": {},
                "Filing Sections": {},
                "Earnings Sections": {},
                "Company Facts": {},
                "Citations": [],
                "Errors": [reason, f"Yahoo SEC filings fallback failed: {exc}"],
            }
        if not isinstance(filings, list):
            return {
                "Status": "unavailable",
                "Provider": "Yahoo Finance SEC filings mirror",
                "Ticker": symbol,
                "Filings": {},
                "Filing Sections": {},
                "Earnings Sections": {},
                "Company Facts": {},
                "Citations": [],
                "Errors": [reason, "Yahoo SEC filings response was malformed"],
            }

        selected = latest_yahoo_filings(filings)
        # The Yahoo-hosted 10-K/10-Q and 8-K documents are independent fetches;
        # both helpers swallow their own errors, so run them concurrently.
        with ThreadPoolExecutor(max_workers=2) as executor:
            sections_future = executor.submit(self.yahoo_filing_sections, selected)
            earnings_future = executor.submit(self.yahoo_earnings_sections, selected)
            sections, section_errors = sections_future.result()
            earnings_sections, earnings_errors = earnings_future.result()
        citations = source_citations(selected, sections, {}, earnings_sections)
        status = "partial" if selected or sections or earnings_sections else "unavailable"
        return {
            "Status": status,
            "Provider": "Yahoo Finance SEC filings mirror",
            "Ticker": symbol,
            "CIK": cik_from_yahoo_filings(selected),
            "Company Name": None,
            "SIC": None,
            "SIC Description": None,
            "Exchanges": [],
            "Filings": selected,
            "Filing Sections": sections,
            "Earnings Sections": earnings_sections,
            "Company Facts": {},
            "Citations": citations,
            "Errors": [
                reason,
                "SEC direct API was unavailable; using Yahoo-hosted copies of SEC filings. "
                "SEC XBRL company facts are not available from this fallback.",
                *section_errors,
                *earnings_errors,
            ],
        }

    def yahoo_filing_sections(
        self, filings: Mapping[str, dict[str, Any]]
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        source = filings.get("10-K") or filings.get("10-Q")
        if not source:
            return {}, ["Yahoo fallback did not include a 10-K or 10-Q filing"]
        url = source.get("url")
        if not isinstance(url, str):
            return {}, ["Yahoo fallback 10-K/10-Q did not include a document URL"]
        try:
            text = self.fetch_text(url)
        except (SecDataError, requests.RequestException) as exc:
            return {}, [f"Could not fetch Yahoo-hosted SEC filing document: {exc}"]

        extracted = extract_filing_sections(text)
        sections = {}
        for label, section in extracted.items():
            sections[label] = {
                **section,
                "Form": source.get("form"),
                "Filing Date": source.get("filing_date"),
                "Report Date": source.get("report_date"),
                "Source URL": url,
            }
        missing = [
            f"{source.get('form')} {spec['item']} {label} section not extracted"
            for label, spec in SECTION_SPECS.items()
            if label not in sections
        ]
        return sections, missing

    def yahoo_earnings_sections(
        self, filings: Mapping[str, dict[str, Any]]
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        source = filings.get("8-K")
        if not source:
            return {}, ["Yahoo fallback did not include an 8-K earnings filing"]
        return self.event_filing_sections(source, source_label="Yahoo-hosted SEC 8-K")

    def cik_for_ticker(self, ticker: str) -> str:
        symbol = clean_ticker(ticker)
        mapping = self.ticker_map()
        row = mapping.get(symbol)
        if not row:
            raise SecDataError(f"No SEC CIK found for {symbol}")
        cik_value = row.get("cik_str")
        if not isinstance(cik_value, int | str):
            raise SecDataError(f"SEC CIK is malformed for {symbol}")
        return str(cik_value).zfill(10)

    def ticker_map(self) -> dict[str, dict[str, Any]]:
        with self._cache_lock:
            if self._ticker_map is not None:
                return self._ticker_map

        payload = self.fetch_json(f"{SEC_WEB_BASE}/files/company_tickers.json")
        if not isinstance(payload, dict):
            raise SecDataError("SEC company ticker map was malformed")
        rows = {}
        for value in payload.values():
            if isinstance(value, dict) and isinstance(value.get("ticker"), str):
                rows[value["ticker"].upper()] = value

        with self._cache_lock:
            self._ticker_map = rows
            return self._ticker_map

    def submissions(self, cik: str) -> dict[str, Any]:
        payload = self.fetch_json(f"{SEC_DATA_BASE}/submissions/CIK{cik}.json")
        if not isinstance(payload, dict):
            raise SecDataError(f"SEC submissions response was malformed for CIK {cik}")
        return payload

    def filing_sections(
        self, filings: Mapping[str, dict[str, Any]]
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        source = filings.get("10-K") or filings.get("10-Q")
        if not source:
            return {}, ["No 10-K or 10-Q filing found in SEC submissions"]
        url = source.get("url")
        if not isinstance(url, str):
            return {}, ["Latest 10-K/10-Q did not include a primary document URL"]
        try:
            text = self.fetch_text(url)
        except requests.RequestException as exc:
            return {}, [f"Could not fetch SEC filing document: {exc}"]

        extracted = extract_filing_sections(text)
        sections = {}
        for label, section in extracted.items():
            sections[label] = {
                **section,
                "Form": source.get("form"),
                "Filing Date": source.get("filing_date"),
                "Report Date": source.get("report_date"),
                "Source URL": url,
            }
        missing = [
            f"{source.get('form')} {spec['item']} {label} section not extracted"
            for label, spec in SECTION_SPECS.items()
            if label not in sections
        ]
        return sections, missing

    def earnings_sections(
        self, filings: Mapping[str, dict[str, Any]]
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        source = filings.get("8-K")
        if not source:
            return {}, ["No 8-K filing found in SEC submissions"]
        return self.event_filing_sections(source, source_label="SEC 8-K")

    def event_filing_sections(
        self, source: Mapping[str, Any], *, source_label: str
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        url = source.get("url")
        if not isinstance(url, str):
            return {}, [f"{source_label} did not include a primary document URL"]
        try:
            text = self.fetch_text(url)
        except (SecDataError, requests.RequestException) as exc:
            return {}, [f"Could not fetch {source_label} document: {exc}"]

        extracted = extract_earnings_sections(text)
        sections = {}
        for label, section in extracted.items():
            sections[label] = {
                **section,
                "Form": source.get("form"),
                "Filing Date": source.get("filing_date"),
                "Report Date": source.get("report_date"),
                "Source URL": url,
            }
        if sections:
            return sections, []
        return {}, [f"{source_label} did not include Item 2.02 or 7.01 earnings/event text"]

    def company_facts(self, cik: str) -> tuple[dict[str, dict[str, Any]], list[str]]:
        try:
            payload = self.fetch_json(f"{SEC_DATA_BASE}/api/xbrl/companyfacts/CIK{cik}.json")
        except requests.RequestException as exc:
            return {}, [f"Could not fetch SEC company facts: {exc}"]
        if not isinstance(payload, dict):
            return {}, ["SEC company facts response was malformed"]

        facts = payload.get("facts")
        if not isinstance(facts, dict):
            return {}, ["SEC company facts response did not include facts"]

        selected: dict[str, dict[str, Any]] = {}
        for label, taxonomy, concepts, preferred_unit in FACT_SPECS:
            fact = select_fact(facts, taxonomy, concepts, preferred_unit)
            if fact:
                selected[label] = fact
        return selected, [] if selected else ["No selected SEC XBRL company facts found"]

    def fetch_json(self, url: str) -> Any:
        cached = self.cached_value(self._json_cache, url)
        if cached is not None:
            return cached

        response = self.fetch(url)
        payload = response.json()
        self.remember_value(self._json_cache, url, payload, ttl_seconds=self.response_cache_seconds)
        return copy.deepcopy(payload)

    def fetch_text(self, url: str) -> str:
        cached = self.cached_value(self._text_cache, url)
        if isinstance(cached, str):
            return cached

        response = self.fetch(url)
        self.remember_value(
            self._text_cache,
            url,
            response.text,
            ttl_seconds=self.response_cache_seconds,
        )
        return str(response.text)

    def fetch(self, url: str) -> requests.Response:
        attempts = max(0, self.max_retries) + 1
        for attempt in range(attempts):
            self.throttle()
            try:
                response = self.session.get(url, headers=self.headers(), timeout=self.timeout)
            except requests.RequestException:
                if attempt + 1 < attempts:
                    self.backoff(None, attempt)
                    continue
                raise

            if response.status_code < 400:
                return response
            if response.status_code in RETRYABLE_STATUS_CODES and attempt + 1 < attempts:
                self.backoff(response, attempt)
                continue
            raise SecDataError(f"SEC request failed with {response.status_code} for {url}")

        raise SecDataError(f"SEC request failed for {url}")

    def throttle(self) -> None:
        _SEC_REQUEST_GATE.wait(self.request_interval_seconds)

    def backoff(self, response: Any | None, attempt: int) -> None:
        retry_after = retry_after_seconds(response)
        if retry_after is None:
            retry_after = self.backoff_base_seconds * (2**attempt)
        delay = min(max(0.0, retry_after), self.backoff_max_seconds)
        if delay > 0:
            time.sleep(delay)

    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        }

    def cached_value(self, cache: dict[str, CacheEntry], key: str) -> Any | None:
        with self._cache_lock:
            entry = cache.get(key)
            if entry is None:
                return None
            if entry.expires_at < time.monotonic():
                cache.pop(key, None)
                return None
            return copy.deepcopy(entry.value)

    def remember_value(
        self,
        cache: dict[str, CacheEntry],
        key: str,
        value: Any,
        *,
        ttl_seconds: float,
    ) -> None:
        if ttl_seconds <= 0:
            return
        with self._cache_lock:
            cache[key] = CacheEntry(
                expires_at=time.monotonic() + ttl_seconds,
                value=copy.deepcopy(value),
            )


def retry_after_seconds(response: Any | None) -> float | None:
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


def latest_filings(submissions: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    filing_block = submissions.get("filings")
    recent = filing_block.get("recent") if isinstance(filing_block, dict) else None
    if not isinstance(recent, dict):
        return {}
    forms = recent.get("form")
    accession_numbers = recent.get("accessionNumber")
    primary_documents = recent.get("primaryDocument")
    filing_dates = recent.get("filingDate")
    report_dates = recent.get("reportDate")
    if (
        not isinstance(forms, list)
        or not isinstance(accession_numbers, list)
        or not isinstance(primary_documents, list)
    ):
        return {}

    selected: dict[str, dict[str, Any]] = {}
    form_rows = list(forms)
    for index, form in enumerate(form_rows):
        if form not in {"10-K", "10-Q", "8-K"} or form in selected:
            continue
        accession = list_value(accession_numbers, index)
        document = list_value(primary_documents, index)
        if not accession or not document:
            continue
        cik = str(submissions.get("cik") or "").lstrip("0")
        filing = {
            "form": form,
            "filing_date": list_value(filing_dates, index),
            "report_date": list_value(report_dates, index),
            "accession_number": accession,
            "primary_document": document,
            "url": filing_url(cik, accession, document),
        }
        selected[form] = filing
        if len(selected) == 3:
            break
    return selected


def latest_yahoo_filings(filings: list[Any]) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for filing in filings:
        if not isinstance(filing, dict):
            continue
        form = filing.get("type")
        if form not in {"10-K", "10-Q", "8-K"} or form in selected:
            continue
        exhibits = filing.get("exhibits")
        if not isinstance(exhibits, dict):
            exhibits = {}
        document = exhibits.get(form)
        if not isinstance(document, str):
            document = next(
                (url for url in exhibits.values() if isinstance(url, str)),
                None,
            )
        if not document:
            continue
        selected[form] = {
            "form": form,
            "filing_date": filing_date_string(filing.get("date")),
            "report_date": None,
            "accession_number": accession_from_url(str(filing.get("edgarUrl") or document)),
            "primary_document": document.rsplit("/", 1)[-1],
            "url": document,
            "source_url": filing.get("edgarUrl"),
            "title": filing.get("title"),
        }
        if len(selected) == 3:
            break
    return selected


def filing_date_string(value: Any) -> str | None:
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value) if value else None


def accession_from_url(url: str) -> str | None:
    match = re.search(r"\b\d{10}-\d{2}-\d{6}\b", url)
    return match.group(0) if match else None


def cik_from_yahoo_filings(filings: Mapping[str, dict[str, Any]]) -> str | None:
    for filing in filings.values():
        for key in ("url", "source_url"):
            value = filing.get(key)
            if not isinstance(value, str):
                continue
            match = re.search(r"/sec-filings/(\d{10})/", value)
            if match:
                return match.group(1)
            suffix = re.search(r"_(\d{1,10})(?:$|[/?#])", value)
            if suffix:
                return suffix.group(1).zfill(10)
    return None


def list_value(values: Any, index: int) -> str | None:
    if isinstance(values, list) and index < len(values):
        value = values[index]
        return str(value) if value else None
    return None


def filing_url(cik_without_padding: str, accession_number: str, document: str) -> str:
    accession_path = accession_number.replace("-", "")
    return f"{SEC_WEB_BASE}/Archives/edgar/data/{cik_without_padding}/{accession_path}/{document}"


def extract_filing_sections(document: str) -> dict[str, dict[str, str]]:
    text = normalize_document_text(document)
    sections = {}
    for label, spec in SECTION_SPECS.items():
        excerpt = extract_between(
            text,
            starts=tuple(spec["starts"]),
            ends=tuple(spec["ends"]),
        )
        if excerpt:
            sections[label] = {
                "Item": str(spec["item"]),
                "Heading": str(spec["heading"]),
                "Snippet": trim_text(excerpt, limit=1800),
            }
    return sections


def extract_earnings_sections(document: str) -> dict[str, dict[str, str]]:
    text = normalize_document_text(document)
    sections = {}
    for label, spec in EARNINGS_SECTION_SPECS.items():
        excerpt = extract_between(
            text,
            starts=tuple(spec["starts"]),
            ends=tuple(spec["ends"]),
            min_length=80,
        )
        if excerpt:
            sections[label] = {
                "Item": str(spec["item"]),
                "Heading": str(spec["heading"]),
                "Snippet": trim_text(excerpt, limit=1400),
            }
    return sections


def normalize_document_text(document: str) -> str:
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", document)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h[1-6]>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_between(
    text: str,
    *,
    starts: tuple[str, ...],
    ends: tuple[str, ...],
    min_length: int = 250,
) -> str | None:
    candidates = []
    for start_pattern in starts:
        for start_match in re.finditer(start_pattern, text, flags=re.IGNORECASE):
            end_index = len(text)
            for end_pattern in ends:
                end_match = re.search(end_pattern, text[start_match.end() :], flags=re.IGNORECASE)
                if end_match:
                    end_index = min(end_index, start_match.end() + end_match.start())
            candidate = text[start_match.start() : end_index].strip()
            if len(candidate) >= min_length:
                candidates.append(candidate)
    if not candidates:
        return None
    return max(candidates, key=len)


def select_fact(
    facts: Mapping[str, Any],
    taxonomy: str,
    concepts: tuple[str, ...],
    preferred_unit: str | None,
) -> dict[str, Any] | None:
    taxonomy_facts = facts.get(taxonomy)
    if not isinstance(taxonomy_facts, dict):
        return None

    for concept in concepts:
        concept_data = taxonomy_facts.get(concept)
        if not isinstance(concept_data, dict):
            continue
        units = concept_data.get("units")
        if not isinstance(units, dict):
            continue
        fact = latest_unit_fact(units, preferred_unit)
        if fact:
            return {
                **fact,
                "Taxonomy": taxonomy,
                "Concept": concept,
                "Label": concept_data.get("label") or concept,
                "Description": trim_text(concept_data.get("description"), limit=300),
            }
    return None


def latest_unit_fact(units: Mapping[str, Any], preferred_unit: str | None) -> dict[str, Any] | None:
    unit_order = list(units)
    if preferred_unit and preferred_unit in units:
        unit_order = [preferred_unit] + [unit for unit in unit_order if unit != preferred_unit]
    for unit in unit_order:
        rows = units.get(unit)
        if not isinstance(rows, list):
            continue
        usable = [
            row
            for row in rows
            if isinstance(row, dict)
            and row.get("val") is not None
            and row.get("form") in {"10-K", "10-Q", "10-K/A", "10-Q/A"}
        ]
        if not usable:
            continue
        latest = max(
            usable,
            key=lambda row: (
                str(row.get("filed") or ""),
                str(row.get("end") or ""),
                int(row.get("fy") or 0),
            ),
        )
        return {
            "Value": latest.get("val"),
            "Unit": unit,
            "Form": latest.get("form"),
            "Filed": latest.get("filed"),
            "Fiscal Year": latest.get("fy"),
            "Fiscal Period": latest.get("fp"),
            "End Date": latest.get("end"),
            "Frame": latest.get("frame"),
            "Accession": latest.get("accn"),
        }
    return None


def source_citations(
    filings: Mapping[str, dict[str, Any]],
    sections: Mapping[str, dict[str, Any]],
    facts: Mapping[str, dict[str, Any]],
    earnings_sections: Mapping[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    for label, section in sections.items():
        citations.append(
            {
                "Label": f"SEC {section.get('Form')} {section.get('Item')} {label}",
                "Type": "filing-section",
                "Form": section.get("Form"),
                "Filing Date": section.get("Filing Date"),
                "Report Date": section.get("Report Date"),
                "URL": section.get("Source URL"),
            }
        )
    for label, section in (earnings_sections or {}).items():
        citations.append(
            {
                "Label": f"SEC {section.get('Form')} {section.get('Item')} {label}",
                "Type": "earnings-section",
                "Form": section.get("Form"),
                "Filing Date": section.get("Filing Date"),
                "Report Date": section.get("Report Date"),
                "URL": section.get("Source URL"),
            }
        )
    facts_url = None
    for filing in filings.values():
        url = filing.get("url")
        if isinstance(url, str):
            facts_url = url.rsplit("/", 1)[0]
            break
    for label, fact in facts.items():
        citations.append(
            {
                "Label": f"SEC XBRL {label}",
                "Type": "company-fact",
                "Form": fact.get("Form"),
                "Filed": fact.get("Filed"),
                "Concept": fact.get("Concept"),
                "Accession": fact.get("Accession"),
                "URL": facts_url,
            }
        )
    return citations
