from __future__ import annotations

import html
import os
import re
import time
from collections.abc import Mapping
from typing import Any

import requests
import yfinance as yf

from app.analysis import trim_text
from app.market_data import clean_ticker

SEC_DATA_BASE = "https://data.sec.gov"
SEC_WEB_BASE = "https://www.sec.gov"
DEFAULT_SEC_USER_AGENT = (
    "The Underlying Analyzer Reboot research app contact:jawauntb@users.noreply.github.com"
)
REQUEST_INTERVAL_SECONDS = 0.12

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


class SecClient:
    def __init__(
        self,
        *,
        session: Any | None = None,
        user_agent: str | None = None,
        timeout: int = 20,
    ) -> None:
        self.session = session or requests.Session()
        self.user_agent = user_agent or os.getenv("SEC_USER_AGENT") or DEFAULT_SEC_USER_AGENT
        self.timeout = timeout
        self._ticker_map: dict[str, dict[str, Any]] | None = None
        self._last_request_at = 0.0

    def get_source_pack(self, ticker: str) -> dict[str, Any]:
        symbol = clean_ticker(ticker)
        try:
            return self.edgar_source_pack(symbol)
        except SecDataError as exc:
            fallback = self.yahoo_source_pack(symbol, reason=str(exc))
            if fallback["Status"] != "unavailable":
                return fallback
            raise

    def edgar_source_pack(self, symbol: str) -> dict[str, Any]:
        cik = self.cik_for_ticker(symbol)
        submissions = self.submissions(cik)
        filings = latest_filings(submissions)
        sections, section_errors = self.filing_sections(filings)
        facts, fact_errors = self.company_facts(cik)
        errors = section_errors + fact_errors
        citations = source_citations(filings, sections, facts)
        status = "available" if sections or facts else "partial" if filings else "unavailable"
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
                "Company Facts": {},
                "Citations": [],
                "Errors": [reason, "Yahoo SEC filings response was malformed"],
            }

        selected = latest_yahoo_filings(filings)
        sections, section_errors = self.yahoo_filing_sections(selected)
        citations = source_citations(selected, sections, {})
        status = "partial" if selected or sections else "unavailable"
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
            "Company Facts": {},
            "Citations": citations,
            "Errors": [
                reason,
                "SEC direct API was unavailable; using Yahoo-hosted copies of SEC filings. "
                "SEC XBRL company facts are not available from this fallback.",
                *section_errors,
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
        if self._ticker_map is None:
            payload = self.fetch_json(f"{SEC_WEB_BASE}/files/company_tickers.json")
            if not isinstance(payload, dict):
                raise SecDataError("SEC company ticker map was malformed")
            rows = {}
            for value in payload.values():
                if isinstance(value, dict) and isinstance(value.get("ticker"), str):
                    rows[value["ticker"].upper()] = value
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
        response = self.fetch(url)
        return response.json()

    def fetch_text(self, url: str) -> str:
        response = self.fetch(url)
        return response.text

    def fetch(self, url: str) -> requests.Response:
        self.throttle()
        response = self.session.get(url, headers=self.headers(), timeout=self.timeout)
        if response.status_code >= 400:
            raise SecDataError(f"SEC request failed with {response.status_code} for {url}")
        return response

    def throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < REQUEST_INTERVAL_SECONDS:
            time.sleep(REQUEST_INTERVAL_SECONDS - elapsed)
        self._last_request_at = time.monotonic()

    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        }


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


def normalize_document_text(document: str) -> str:
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", document)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h[1-6]>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_between(text: str, *, starts: tuple[str, ...], ends: tuple[str, ...]) -> str | None:
    candidates = []
    for start_pattern in starts:
        for start_match in re.finditer(start_pattern, text, flags=re.IGNORECASE):
            end_index = len(text)
            for end_pattern in ends:
                end_match = re.search(end_pattern, text[start_match.end() :], flags=re.IGNORECASE)
                if end_match:
                    end_index = min(end_index, start_match.end() + end_match.start())
            candidate = text[start_match.start() : end_index].strip()
            if len(candidate) >= 250:
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
