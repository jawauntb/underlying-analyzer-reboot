"""SEC 10-K / 10-Q sections, per-filing summaries and a cross-filing synthesis.

:mod:`app.sec` already knows how to find a company's CIK, read its submissions
index and slice Item 1 / Item 1A / Item 7 out of a filing document. What it does
not do is walk *back* through the index — ``latest_filings`` returns only the most
recent 10-K and 10-Q, and trims each section to 1,800 characters for a one-page
brief. Prism wants the last two annual reports and the last three quarterlies at
roughly 12,000 characters a section, which is the bounded input a language model
can actually reason over.

So this module reuses ``sec.filing_url``, ``sec.normalize_document_text`` and
``sec.SECTION_SPECS`` for the parsing and does its own index walk on top.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

SECTION_LABELS: tuple[str, ...] = ("Business", "Risk Factors", "MD&A")

#: Packet key for each SEC section label.
SECTION_KEYS: dict[str, str] = {
    "Business": "business",
    "Risk Factors": "risk_factors",
    "MD&A": "mdna",
}

#: A 10-Q numbers its items differently from a 10-K: MD&A is Part I Item 2 (not
#: Item 7) and the risk factors sit in Part II Item 1A, followed by "Unregistered
#: Sales" rather than "Properties". Reusing the 10-K patterns on a quarterly
#: report finds the table-of-contents entry, fails to find a terminator, and
#: returns the financial statements instead - so the quarterly report gets its
#: own spec set, in the same shape as :data:`app.sec.SECTION_SPECS`.
TENQ_SECTION_SPECS: dict[str, dict[str, Any]] = {
    "risk_factors": {
        "select": "last",
        "starts": [r"\bItem\s+1A\.?\s+Risk\s+Factors\b"],
        "ends": [
            r"\bItem\s+2\.?\s+Unregistered\s+Sales\b",
            r"\bItem\s+3\.?\s+Defaults\b",
            r"\bItem\s+4\.?\s+Mine\s+Safety\b",
            r"\bItem\s+5\.?\s+Other\s+Information\b",
            r"\bItem\s+6\.?\s+Exhibits\b",
            r"\bSIGNATURES?\b",
        ],
    },
    "mdna": {
        "select": "last",
        "starts": [
            r"\bItem\s+2\.?\s+Management[\u2019']?s\s+Discussion\s+and\s+Analysis\b"
        ],
        # No "PART II" terminator: a 10-Q's MD&A routinely cross-references
        # "Part II, Item 1A. Risk Factors" in its first paragraph, and that
        # reference cut the section off at the forward-looking-statements
        # preamble, before any actual discussion of results. Part I Item 3 and
        # Item 4 always follow Item 2, so they are the honest terminators.
        "ends": [
            r"\bItem\s+3\.?\s+Quantitative\s+and\s+Qualitative\b",
            r"\bItem\s+4\.?\s+Controls\s+and\s+Procedures\b",
        ],
    },
}

DEFAULT_SECTION_CHARS = 12_000
MIN_SECTION_LENGTH = 1_500
#: Below this a match is a passing mention, not a section (mirrors app.sec).
FLOOR_SECTION_LENGTH = 250
DEFAULT_MAX_10K = 2
DEFAULT_MAX_10Q = 3
DEFAULT_MAX_WORKERS = 3

SYNTHESIS_KEYS: tuple[str, ...] = (
    "performance",
    "risks",
    "growth_opportunities",
    "new_business_lines",
    "operating_context",
    "capex_suppliers_customers",
)

FILING_SUMMARY_SYSTEM = (
    "You are a securities analyst reading one SEC filing. Summarise only what the "
    "filing text actually says. Quote figures exactly as filed and never invent a "
    "number, a date, or a business line that is not in the excerpt. If a topic is "
    "not covered by the excerpt, say so plainly. No investment advice."
)

SYNTHESIS_SYSTEM = (
    "You are a securities analyst comparing several SEC filings from the same "
    "company across time. Return strict JSON only - no prose outside the JSON "
    "object. Every claim must be traceable to the supplied filing excerpts and "
    "summaries; where the filings disagree or are silent, say so. No investment "
    "advice."
)


def _trim(text: Any, *, limit: int) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3].rstrip()}..."


def collect_filings(
    submissions: Mapping[str, Any],
    *,
    max_10k: int = DEFAULT_MAX_10K,
    max_10q: int = DEFAULT_MAX_10Q,
) -> list[dict[str, Any]]:
    """Walk the submissions index for the most recent 10-K and 10-Q filings.

    ``app.sec.latest_filings`` stops at the first of each form; this keeps going
    until both quotas are filled, so the synthesis can compare this year's risk
    factors against last year's.
    """
    from app.sec import filing_url

    block = submissions.get("filings")
    recent = block.get("recent") if isinstance(block, Mapping) else None
    if not isinstance(recent, Mapping):
        return []
    forms = recent.get("form")
    accessions = recent.get("accessionNumber")
    documents = recent.get("primaryDocument")
    filing_dates = recent.get("filingDate")
    report_dates = recent.get("reportDate")
    if not isinstance(forms, list) or not isinstance(accessions, list):
        return []
    if not isinstance(documents, list):
        return []

    cik = str(submissions.get("cik") or "").lstrip("0")
    wanted = {"10-K": max(0, int(max_10k)), "10-Q": max(0, int(max_10q))}
    found: dict[str, int] = {"10-K": 0, "10-Q": 0}
    selected: list[dict[str, Any]] = []

    def _value(rows: Any, index: int) -> str | None:
        if isinstance(rows, list) and 0 <= index < len(rows):
            value = rows[index]
            return str(value) if isinstance(value, str) and value else None
        return None

    for index, form in enumerate(forms):
        if form not in wanted or found[form] >= wanted[form]:
            continue
        accession = _value(accessions, index)
        document = _value(documents, index)
        if not accession or not document:
            continue
        found[form] += 1
        selected.append(
            {
                "form": str(form),
                "filing_date": _value(filing_dates, index),
                "report_date": _value(report_dates, index),
                "accession_number": accession,
                "primary_document": document,
                "url": filing_url(cik, accession, document),
            }
        )
        if all(found[key] >= wanted[key] for key in wanted):
            break
    return selected


def section_specs_for(form: str) -> dict[str, dict[str, Any]]:
    """The item patterns that apply to one form, keyed by packet section name."""
    from app.sec import SECTION_SPECS

    if str(form or "").upper().startswith("10-Q"):
        return TENQ_SECTION_SPECS
    return {
        SECTION_KEYS.get(label, label): {
            "select": "longest",
            "starts": list(spec["starts"]),
            "ends": list(spec["ends"]),
        }
        for label, spec in SECTION_SPECS.items()
    }


def select_section(
    text: str,
    *,
    starts: Sequence[str],
    ends: Sequence[str],
    min_length: int = MIN_SECTION_LENGTH,
) -> str | None:
    """The last heading occurrence that is actually followed by the next item.

    ``app.sec.extract_between`` keeps the *longest* candidate. In a 10-K that is
    right: the body section beats the table-of-contents line, and the stray
    cross-references appear after the section they point at. In a 10-Q it is
    wrong: Part II Item 1A sits near the end, and an earlier cross-reference
    ("see Item 1A. Risk Factors for ...") produces a candidate that swallows the
    real section and is therefore longer. So the quarterly spec set asks for the
    last occurrence that has a terminator, and the annual one keeps the longest.
    """
    candidates: list[tuple[int, str]] = []
    for start_pattern in starts:
        for match in re.finditer(start_pattern, text, flags=re.IGNORECASE):
            tail = text[match.end() :]
            end_offset: int | None = None
            for end_pattern in ends:
                found = re.search(end_pattern, tail, flags=re.IGNORECASE)
                if found:
                    end_offset = (
                        found.start() if end_offset is None else min(end_offset, found.start())
                    )
            if end_offset is None:
                continue
            candidate = text[match.start() : match.end() + end_offset].strip()
            if len(candidate) >= FLOOR_SECTION_LENGTH:
                candidates.append((match.start(), candidate))
    if not candidates:
        return None
    # A substantial section is preferred, but a quarterly Item 1A that only says
    # "no material changes from the risk factors in our Form 10-K" is a real
    # section and a real answer, so a short last occurrence beats nothing.
    substantial = [item for item in candidates if len(item[1]) >= min_length]
    return max(substantial or candidates, key=lambda item: item[0])[1]


def extract_sections(
    document: str, *, form: str = "10-K", limit: int = DEFAULT_SECTION_CHARS
) -> dict[str, str]:
    """Business / Risk Factors / MD&A text, bounded at ``limit`` characters each.

    Reuses ``app.sec``'s annual-report patterns and normaliser, with the
    quarterly item numbering supplied by :data:`TENQ_SECTION_SPECS`, and keeps far
    more of each section than the terminal's one-page brief does.
    """
    from app.sec import extract_between, normalize_document_text

    text = normalize_document_text(document)
    sections: dict[str, str] = {}
    for key, spec in section_specs_for(form).items():
        starts = tuple(spec["starts"])
        ends = tuple(spec["ends"])
        excerpt: str | None = None
        if spec.get("select") == "last":
            excerpt = select_section(text, starts=starts, ends=ends)
        if excerpt is None:
            excerpt = extract_between(text, starts=starts, ends=ends)
        if excerpt:
            sections[key] = _trim(excerpt, limit=limit)
    return sections


def fetch_filing(
    sec_client: Any,
    filing: Mapping[str, Any],
    *,
    section_chars: int = DEFAULT_SECTION_CHARS,
) -> dict[str, Any]:
    """Fetch one filing document and slice its sections; errors stay in the row."""
    row: dict[str, Any] = {
        "form": filing.get("form"),
        "filing_date": filing.get("filing_date"),
        "report_date": filing.get("report_date"),
        "accession_number": filing.get("accession_number"),
        "url": filing.get("url"),
        "sections": {},
        "section_chars": {},
        "summary": None,
        "error": None,
    }
    url = filing.get("url")
    if not isinstance(url, str) or not url:
        row["error"] = "filing had no primary document URL"
        return row
    try:
        document = sec_client.fetch_text(url)
    except Exception as exc:  # noqa: BLE001 - a dead document must not sink the section
        row["error"] = f"could not fetch filing document: {exc}"
        return row
    sections = extract_sections(
        document, form=str(filing.get("form") or "10-K"), limit=section_chars
    )
    row["sections"] = sections
    row["section_chars"] = {key: len(value) for key, value in sections.items()}
    if not sections:
        row["error"] = "no business / risk-factor / MD&A section could be extracted"
    return row


def summarize_filing(
    row: Mapping[str, Any],
    *,
    ticker: str,
    text_generator: Any,
    max_tokens: int = 700,
    input_chars: int = 12_000,
) -> str | None:
    """One bounded LLM summary of a single filing, or ``None`` on failure."""
    sections = row.get("sections") or {}
    if not isinstance(sections, Mapping) or not sections:
        return None
    per_section = input_chars // max(1, len(sections))
    parts = [
        f"## {key.replace('_', ' ').title()}\n{_trim(value, limit=per_section)}"
        for key, value in sections.items()
    ]
    prompt = (
        f"Company: {ticker}\n"
        f"Filing: {row.get('form')} filed {row.get('filing_date')} "
        f"for the period ending {row.get('report_date')}\n"
        f"Source: {row.get('url')}\n\n"
        + "\n\n".join(parts)
        + "\n\nWrite 150-220 words covering: what the business does and how the mix is "
        "changing, the reported performance and its drivers, and the two or three "
        "risks the filing itself emphasises. Plain prose, no bullet points, no advice."
    )
    try:
        generated = text_generator.generate_text(
            system=FILING_SUMMARY_SYSTEM,
            prompt=prompt,
            max_tokens=int(max_tokens),
            temperature=0.1,
        )
    except Exception:  # noqa: BLE001 - the deterministic excerpt still ships
        return None
    text = getattr(generated, "text", None)
    return str(text).strip() if text else None


def deterministic_summary(row: Mapping[str, Any]) -> str | None:
    """Excerpt-based summary used when no text generator is configured."""
    sections = row.get("sections") or {}
    if not isinstance(sections, Mapping) or not sections:
        return None
    lead = sections.get("mdna") or sections.get("business") or next(iter(sections.values()))
    return (
        f"{row.get('form')} filed {row.get('filing_date')} "
        f"(period ending {row.get('report_date')}). Excerpt: {_trim(lead, limit=900)}"
    )


def _synthesis_prompt(ticker: str, rows: Sequence[Mapping[str, Any]], *, budget: int) -> str:
    per_filing = max(800, budget // max(1, len(rows)))
    blocks: list[str] = []
    for row in rows:
        sections = row.get("sections") or {}
        section_budget = max(300, per_filing // max(1, len(sections) + 1))
        body = "\n".join(
            f"{key.replace('_', ' ').title()}: {_trim(value, limit=section_budget)}"
            for key, value in sections.items()
        )
        blocks.append(
            f"### {row.get('form')} filed {row.get('filing_date')} "
            f"(period ending {row.get('report_date')}) {row.get('url')}\n"
            f"Summary: {_trim(row.get('summary'), limit=900)}\n{body}"
        )
    keys = ", ".join(f'"{key}"' for key in SYNTHESIS_KEYS)
    return (
        f"Company: {ticker}\n"
        f"Filings, newest first:\n\n" + "\n\n".join(blocks) + "\n\n"
        "Return a JSON object with exactly these string-valued keys: "
        f"{keys}. Each value is 2-5 sentences of plain prose. "
        "'performance' covers revenue, margins and cash generation as reported and "
        "how they changed across the filings. 'risks' covers the risks the filings "
        "themselves emphasise and any that were added or dropped between filings. "
        "'growth_opportunities' covers stated growth drivers. 'new_business_lines' "
        "covers segments, products or markets that appear or expand across filings - "
        "say 'none disclosed across these filings' if there are none. "
        "'operating_context' covers demand, competition, supply and regulation as the "
        "filings describe them. 'capex_suppliers_customers' covers capital spending, "
        "named suppliers or manufacturing partners, and customer concentration. "
        "JSON only."
    )


def parse_synthesis(text: str) -> dict[str, Any] | None:
    """Pull the JSON object out of a model reply, tolerating code fences."""
    if not text:
        return None
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, flags=re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        start = candidate.find("{")
        if start == -1:
            return None
        end = candidate.rfind("}")
        # No closing brace means the reply hit the token limit mid-object; keep
        # the tail so the salvage below can still read the keys that finished.
        candidate = candidate[start : end + 1] if end > start else candidate[start:]
    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError):
        # A reply cut off at the token limit is still worth most of its content:
        # every key that closed its string is recoverable, and reporting five of
        # six sections beats falling back to raw excerpts for all six.
        salvaged = _salvage_synthesis(candidate)
        return salvaged or None
    if not isinstance(parsed, dict):
        return None
    return {key: parsed.get(key) for key in SYNTHESIS_KEYS if parsed.get(key)}


def _salvage_synthesis(candidate: str) -> dict[str, Any]:
    """Recover the completed ``"key": "value"`` pairs of a truncated JSON reply."""
    recovered: dict[str, Any] = {}
    for key in SYNTHESIS_KEYS:
        match = re.search(
            rf'"{re.escape(key)}"\s*:\s*("(?:[^"\\]|\\.)*")', candidate, flags=re.DOTALL
        )
        if not match:
            continue
        try:
            value = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(value, str) and value.strip():
            recovered[key] = value
    if recovered:
        recovered["truncated"] = True
    return recovered


def deterministic_synthesis(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Filing-excerpt synthesis used when no model is available."""
    def _first(section_key: str, limit: int = 700) -> str | None:
        for row in rows:
            sections = row.get("sections") or {}
            if isinstance(sections, Mapping) and sections.get(section_key):
                return _trim(sections[section_key], limit=limit)
        return None

    mdna = _first("mdna")
    business = _first("business")
    risks = _first("risk_factors")
    forms = ", ".join(
        f"{row.get('form')} ({row.get('filing_date')})" for row in rows if row.get("form")
    )
    note = "Excerpted directly from the filings; no model summary was generated."
    return {
        "performance": f"{mdna} [{note}]" if mdna else f"No MD&A section extracted from {forms}.",
        "risks": (
            f"{risks} [{note}]" if risks else f"No risk-factor section extracted from {forms}."
        ),
        "growth_opportunities": (
            f"{business} [{note}]" if business else f"No business section extracted from {forms}."
        ),
        "new_business_lines": "Not determinable without a model summary of the filing text.",
        "operating_context": (
            f"{business} [{note}]" if business else "No operating context extracted."
        ),
        "capex_suppliers_customers": (
            "Not determinable without a model summary of the filing text."
        ),
        "method": "deterministic_excerpt",
    }


def synthesize(
    rows: Sequence[Mapping[str, Any]],
    *,
    ticker: str,
    text_generator: Any | None,
    max_tokens: int = 3200,
    budget: int = 18_000,
) -> dict[str, Any]:
    """Cross-filing synthesis; falls back to excerpts when the model is absent."""
    usable = [row for row in rows if row.get("sections")]
    if not usable:
        return dict.fromkeys(SYNTHESIS_KEYS) | {
            "method": "unavailable",
            "error": "no filing sections were extracted",
        }
    if text_generator is None:
        return deterministic_synthesis(usable)
    try:
        generated = text_generator.generate_text(
            system=SYNTHESIS_SYSTEM,
            prompt=_synthesis_prompt(ticker, usable, budget=budget),
            max_tokens=int(max_tokens),
            temperature=0.1,
        )
    except Exception as exc:  # noqa: BLE001
        fallback = deterministic_synthesis(usable)
        fallback["error"] = f"model synthesis failed: {exc}"
        return fallback
    parsed = parse_synthesis(str(getattr(generated, "text", "") or ""))
    if not parsed:
        fallback = deterministic_synthesis(usable)
        fallback["error"] = "model synthesis did not return parseable JSON"
        return fallback
    parsed.setdefault("method", "model")
    parsed["model"] = getattr(generated, "model", None)
    for key in SYNTHESIS_KEYS:
        parsed.setdefault(key, None)
    return parsed


def build_filings(
    sec_client: Any | None,
    ticker: str,
    *,
    text_generator: Any | None = None,
    max_10k: int = DEFAULT_MAX_10K,
    max_10q: int = DEFAULT_MAX_10Q,
    section_chars: int = DEFAULT_SECTION_CHARS,
    max_workers: int = DEFAULT_MAX_WORKERS,
    summarize: bool = True,
) -> dict[str, Any]:
    """Build ``packet["filings"]``.

    Never raises. A missing CIK, an unreachable document or an offline model each
    degrade one part of the section and are recorded in ``errors``.
    """
    symbol = str(ticker or "").strip().upper()
    section: dict[str, Any] = {
        "provider": "sec_edgar",
        "fetched_at": datetime.now(UTC).isoformat(),
        "ticker": symbol,
        "cik": None,
        "ten_k": [],
        "ten_q": [],
        "synthesis": dict.fromkeys(SYNTHESIS_KEYS) | {"method": "unavailable"},
        "errors": [],
    }
    if sec_client is None:
        section["errors"].append("SEC client is not configured")
        return section

    try:
        cik = sec_client.cik_for_ticker(symbol)
    except Exception as exc:  # noqa: BLE001
        section["errors"].append(f"CIK lookup failed: {exc}")
        return section
    section["cik"] = cik

    try:
        submissions = sec_client.submissions(cik)
    except Exception as exc:  # noqa: BLE001
        section["errors"].append(f"submissions fetch failed: {exc}")
        return section

    filings = collect_filings(submissions, max_10k=max_10k, max_10q=max_10q)
    if not filings:
        section["errors"].append("no 10-K or 10-Q filings found in the submissions index")
        return section

    workers = max(1, min(int(max_workers), len(filings)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(
            pool.map(
                lambda filing: fetch_filing(sec_client, filing, section_chars=section_chars),
                filings,
            )
        )

    for row in rows:
        if row.get("error"):
            section["errors"].append(f"{row.get('form')} {row.get('filing_date')}: {row['error']}")

    if summarize and text_generator is not None:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            summaries = list(
                pool.map(
                    lambda row: summarize_filing(
                        row, ticker=symbol, text_generator=text_generator
                    ),
                    rows,
                )
            )
        for row, summary in zip(rows, summaries, strict=True):
            row["summary"] = summary or deterministic_summary(row)
            row["summary_method"] = "model" if summary else "excerpt"
    else:
        for row in rows:
            row["summary"] = deterministic_summary(row)
            row["summary_method"] = "excerpt"
            if summarize and text_generator is None:
                row.setdefault("summary_note", "no text generator configured")

    section["ten_k"] = [row for row in rows if row.get("form") == "10-K"]
    section["ten_q"] = [row for row in rows if row.get("form") == "10-Q"]
    section["synthesis"] = synthesize(
        rows, ticker=symbol, text_generator=text_generator if summarize else None
    )
    if isinstance(section["synthesis"], dict) and section["synthesis"].get("error"):
        section["errors"].append(str(section["synthesis"]["error"]))
    return section
