"""Filing-change and news evidence for Situate (SPEC 5.6).

The premise (Cohen, Malloy & Nguyen 2020, "Lazy Prices") is that *changes* to a
company's disclosures carry information that a summary does not: firms that
rewrite their filings tend to underperform. So this module never asks a model to
summarise a 10-K. It:

1. pulls the last two 10-Ks and last three 10-Qs via Prism's :class:`SecClient`,
   filtered ``filing_date <= t`` (point-in-time);
2. **diffs** each of five sections — Item 1A Risk Factors, Item 7 MD&A, and the
   customer/supplier-concentration, capex and guidance passages — against the
   prior comparable filing, scoring each with ``change_score = 1 - cosine`` of a
   numpy hashing-vectorised token count plus added/removed sentence counts;
3. prompts the Anthropic text client with **only the diff** (the added and
   removed passages, never the whole document) at temperature 0 for JSON: new and
   removed risks *with quotes*, concentration/capex/guidance changes, and a 1-5
   material-change score. The model's output is **evidence**, recorded and cited,
   and is never fed into any numeric forecast;
4. clusters recent news (Exa + Massive), ``published <= t``, into dated events
   with a sentiment read and cross-asset exposure flags.

Every prompt and raw response is persisted in the section so a reader can audit
exactly what the model saw. Nothing here fabricates: a section that will not
extract, a filing that will not fetch, or a model call that fails becomes an
entry in ``errors``, not an invented number.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np

__all__ = [
    "MODULE_VERSION",
    "TextError",
    "build_text_section",
    "change_score",
    "cluster_events",
    "cosine_similarity",
    "extract_sections",
    "hashing_vector",
    "sentence_diff",
]

MODULE_VERSION = "1.0.0"

#: Hashing-vectoriser dimensionality (no new deps: a plain numpy count vector).
VECTOR_DIM = 1 << 18
#: How far back news is considered, in days.
NEWS_LOOKBACK_DAYS = 90
#: Cap on the diff text handed to the model, in characters (added + removed).
MAX_DIFF_CHARS = 12000
#: Cap on a stored section excerpt, in characters.
MAX_SECTION_CHARS = 40000

#: Sections diffed in every filing, in report order.
SECTION_LABELS: tuple[str, ...] = (
    "Risk Factors",
    "MD&A",
    "Customer/Supplier Concentration",
    "Capex",
    "Guidance",
)

CONCENTRATION_KEYWORDS: tuple[str, ...] = (
    "concentration",
    "one customer",
    "single customer",
    "largest customer",
    "significant customer",
    "major customer",
    "supplier",
    "sole source",
    "single source",
    "accounted for",
    "% of net revenue",
    "% of revenue",
    "% of total revenue",
    "% of our revenue",
)
CAPEX_KEYWORDS: tuple[str, ...] = (
    "capital expenditure",
    "capital expenditures",
    "capex",
    "purchases of property",
    "property and equipment",
    "property, plant",
    "construction in progress",
    "capital spending",
    "capital investment",
)
GUIDANCE_KEYWORDS: tuple[str, ...] = (
    "we expect",
    "we anticipate",
    "outlook",
    "guidance",
    "we believe",
    "for the full year",
    "for fiscal",
    "next quarter",
    "in the coming",
    "going forward",
    "we intend",
    "we plan",
)

_POSITIVE_WORDS = frozenset(
    {
        "beat",
        "beats",
        "record",
        "growth",
        "surge",
        "surged",
        "jumps",
        "jumped",
        "gains",
        "gain",
        "upgrade",
        "upgraded",
        "outperform",
        "strong",
        "raises",
        "raised",
        "boost",
        "profit",
        "wins",
        "win",
        "approval",
        "approved",
        "expands",
        "expansion",
        "rally",
        "bullish",
        "beat expectations",
        "tops",
        "soars",
    }
)
_NEGATIVE_WORDS = frozenset(
    {
        "miss",
        "misses",
        "missed",
        "cut",
        "cuts",
        "downgrade",
        "downgraded",
        "falls",
        "fell",
        "plunge",
        "plunged",
        "slump",
        "weak",
        "warning",
        "warns",
        "lawsuit",
        "probe",
        "investigation",
        "recall",
        "delay",
        "delayed",
        "loss",
        "losses",
        "layoffs",
        "bearish",
        "slashes",
        "slashed",
        "decline",
        "declines",
        "concerns",
        "fraud",
        "halt",
        "halted",
    }
)

#: Keyword -> cross-asset exposure flag surfaced to the memo.
_EXPOSURE_FLAG_KEYWORDS: tuple[tuple[str, str], ...] = (
    (r"\b(rate hike|interest rate|fed|federal reserve|yields?|treasur)", "rates_sensitive"),
    (r"\b(tariff|export control|sanction|geopolit|china|taiwan)", "geopolitical"),
    (r"\b(dollar|currency|forex|fx|exchange rate|yen|euro)", "fx_exposure"),
    (r"\b(supply chain|shortage|supplier|foundry|capacity)", "supply_chain"),
    (r"\b(oil|energy price|commodity|commodities|copper|gold)", "commodity_exposure"),
    (r"\b(regulat|antitrust|ftc|doj|sec charges|compliance)", "regulatory"),
    (r"\b(recession|inflation|cpi|gdp|macro|demand weak)", "macro_sensitive"),
)

_EVENT_TYPE_KEYWORDS: tuple[tuple[str, str], ...] = (
    (r"\b(earnings|quarter(ly)? results|eps|revenue beat|reports q)", "earnings"),
    (r"\b(guidance|outlook|forecast|raises|lowers|cuts view)", "guidance"),
    (
        r"\b(lawsuit|probe|investigation|antitrust|ftc|doj|settle|tariff|regulat)",
        "legal_regulatory",
    ),
    (r"\b(ceo|cfo|executive|resign|appoint|board|management)", "management"),
    (r"\b(upgrade|downgrade|price target|analyst|rating|initiat)", "analyst"),
    (r"\b(acquire|acquisition|merger|deal|buyout|stake|invest)", "deal"),
    (r"\b(dividend|buyback|repurchase|split)", "capital_return"),
    (r"\b(launch|unveil|new product|releases?|product line)", "product"),
)


class TextError(RuntimeError):
    """Raised when the text section cannot be built at all."""


# --------------------------------------------------------------------------- #
# Hashing vectoriser + change score.
# --------------------------------------------------------------------------- #
_TOKEN_RE = re.compile(r"[a-z0-9%]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(str(text or "").lower())


def _bucket(token: str) -> int:
    digest = hashlib.md5(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % VECTOR_DIM


def hashing_vector(text: str, *, dim: int = VECTOR_DIM) -> np.ndarray:
    """Deterministic numpy hashing-vectoriser count vector (no external deps)."""
    vector = np.zeros(dim, dtype=np.float64)
    for token in _tokens(text):
        vector[_bucket(token) if dim == VECTOR_DIM else (_bucket(token) % dim)] += 1.0
    return vector


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine of two count vectors; 0.0 when either is empty."""
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def change_score(before: str, after: str) -> float:
    """``1 - cosine`` of the two texts' token vectors, in ``[0, 1]``.

    ``0`` means identical wording; ``1`` means no shared tokens. Two empty texts
    are treated as unchanged (``0``); a text appearing or disappearing entirely
    is a full change (``1``).
    """
    before = str(before or "")
    after = str(after or "")
    if not before.strip() and not after.strip():
        return 0.0
    if not before.strip() or not after.strip():
        return 1.0
    similarity = cosine_similarity(hashing_vector(before), hashing_vector(after))
    return float(max(0.0, min(1.0, 1.0 - similarity)))


# --------------------------------------------------------------------------- #
# Sentence diff.
# --------------------------------------------------------------------------- #
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _sentences(text: str) -> list[str]:
    raw = _SENTENCE_RE.split(" ".join(str(text or "").split()))
    return [s.strip() for s in raw if len(s.strip()) >= 20]


def _normalise_sentence(sentence: str) -> str:
    return " ".join(_tokens(sentence))


def sentence_diff(before: str, after: str) -> tuple[list[str], list[str]]:
    """Return ``(added, removed)`` sentences comparing ``after`` against ``before``.

    Sentences are matched on their normalised token stream so pure whitespace or
    casing changes are not counted as edits.
    """
    before_sentences = _sentences(before)
    after_sentences = _sentences(after)
    before_keys = {_normalise_sentence(s) for s in before_sentences}
    after_keys = {_normalise_sentence(s) for s in after_sentences}
    added = [s for s in after_sentences if _normalise_sentence(s) not in before_keys]
    removed = [s for s in before_sentences if _normalise_sentence(s) not in after_keys]
    return added, removed


# --------------------------------------------------------------------------- #
# Section extraction.
# --------------------------------------------------------------------------- #
def _keyword_paragraphs(
    text: str, keywords: Sequence[str], *, limit: int = MAX_SECTION_CHARS
) -> str:
    """Sentences mentioning any keyword, concatenated (topical pseudo-section)."""
    lowered_keywords = [k.lower() for k in keywords]
    kept: list[str] = []
    for sentence in _sentences(text):
        low = sentence.lower()
        if any(keyword in low for keyword in lowered_keywords):
            kept.append(sentence)
    joined = " ".join(kept)
    return joined[:limit]


def extract_sections(document_text: str) -> dict[str, str]:
    """Extract the five diffable sections from a filing's raw document text."""
    from app.sec import SECTION_SPECS, extract_between, normalize_document_text

    text = normalize_document_text(document_text)
    sections: dict[str, str] = {}
    for label, spec_label in (("Risk Factors", "Risk Factors"), ("MD&A", "MD&A")):
        spec = SECTION_SPECS.get(spec_label)
        excerpt = ""
        if spec:
            found = extract_between(
                text,
                starts=tuple(spec["starts"]),
                ends=tuple(spec["ends"]),
                min_length=200,
            )
            excerpt = (found or "")[:MAX_SECTION_CHARS]
        sections[label] = excerpt
    sections["Customer/Supplier Concentration"] = _keyword_paragraphs(text, CONCENTRATION_KEYWORDS)
    sections["Capex"] = _keyword_paragraphs(text, CAPEX_KEYWORDS)
    sections["Guidance"] = _keyword_paragraphs(text, GUIDANCE_KEYWORDS)
    return sections


# --------------------------------------------------------------------------- #
# Filing selection (point-in-time).
# --------------------------------------------------------------------------- #
def _resolve_as_of(as_of: date | str | None) -> date:
    if as_of is None:
        return datetime.now(UTC).date()
    if isinstance(as_of, date):
        return as_of
    return datetime.strptime(str(as_of)[:10], "%Y-%m-%d").date()


def _parse_iso(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def select_filings(
    submissions: Mapping[str, Any],
    *,
    as_of: date,
    wanted: Mapping[str, int] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Select the most recent ``wanted`` filings per form, ``filing_date <= t``.

    Returns ``{form: [filing, ...]}`` newest-first, each filing carrying its form,
    filing/report dates, accession number, primary document and resolved URL.
    """
    from app.sec import filing_url

    wanted = wanted or {"10-K": 2, "10-Q": 3}
    filing_block = submissions.get("filings")
    recent = filing_block.get("recent") if isinstance(filing_block, Mapping) else None
    if not isinstance(recent, Mapping):
        return {form: [] for form in wanted}
    forms = recent.get("form")
    accession_numbers = recent.get("accessionNumber")
    primary_documents = recent.get("primaryDocument")
    filing_dates = recent.get("filingDate")
    report_dates = recent.get("reportDate")
    if not (isinstance(forms, list) and isinstance(accession_numbers, list)):
        return {form: [] for form in wanted}

    cik = str(submissions.get("cik") or "").lstrip("0")
    selected: dict[str, list[dict[str, Any]]] = {form: [] for form in wanted}
    for index, form in enumerate(forms):
        if form not in selected or len(selected[form]) >= wanted[form]:
            continue
        filed = _parse_iso(_list_value(filing_dates, index))
        if filed is None or filed > as_of:
            continue
        accession = _list_value(accession_numbers, index)
        document = _list_value(primary_documents, index)
        if not accession or not document:
            continue
        selected[form].append(
            {
                "form": form,
                "filing_date": _list_value(filing_dates, index),
                "report_date": _list_value(report_dates, index),
                "accession_number": accession,
                "primary_document": document,
                "url": filing_url(cik, accession, document),
            }
        )
        if all(len(selected[f]) >= wanted[f] for f in wanted):
            break
    return selected


def _list_value(values: Any, index: int) -> str | None:
    if isinstance(values, list) and index < len(values):
        value = values[index]
        return str(value) if value else None
    return None


# --------------------------------------------------------------------------- #
# Filing diff pipeline.
# --------------------------------------------------------------------------- #
def _diff_pair(
    current: Mapping[str, Any],
    prior: Mapping[str, Any],
    current_sections: Mapping[str, str],
    prior_sections: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Per-section numeric diff between one filing and its prior comparable."""
    entries: list[dict[str, Any]] = []
    for label in SECTION_LABELS:
        before = prior_sections.get(label, "")
        after = current_sections.get(label, "")
        added, removed = sentence_diff(before, after)
        entries.append(
            {
                "section": label,
                "form": current.get("form"),
                "filing_date": current.get("filing_date"),
                "prior_filing_date": prior.get("filing_date"),
                "change_score": round(change_score(before, after), 4),
                "added_sentences": len(added),
                "removed_sentences": len(removed),
                "new_risks": None,
                "removed_risks": None,
                "concentration_change": None,
                "capex_change": None,
                "guidance_tone_change": None,
                "material_change_score": None,
                "_added": added,
                "_removed": removed,
            }
        )
    return entries


def _diff_payload(entries: Sequence[Mapping[str, Any]]) -> str:
    """Assemble the added/removed passages the model is allowed to see."""
    chunks: list[str] = []
    for entry in entries:
        added = entry.get("_added") or []
        removed = entry.get("_removed") or []
        if not added and not removed:
            continue
        chunks.append(f"### Section: {entry['section']}")
        if added:
            chunks.append("ADDED passages:\n" + "\n".join(f"- {s}" for s in added[:20]))
        if removed:
            chunks.append("REMOVED passages:\n" + "\n".join(f"- {s}" for s in removed[:20]))
    return "\n\n".join(chunks)[:MAX_DIFF_CHARS]


_LLM_SYSTEM = (
    "You are a forensic filing-diff analyst. You are shown ONLY the passages that "
    "were ADDED to or REMOVED from a company's SEC filing relative to its prior "
    "comparable filing -- never the full document. Report only what these diffs "
    "support. Quote the exact added/removed text as evidence for every claim. Do "
    "not forecast prices, do not give investment advice, and do not infer "
    "anything not present in the diff. Respond with a single JSON object and no "
    "other text."
)

_LLM_INSTRUCTIONS = (
    "From the diff below produce a JSON object with exactly these keys:\n"
    '  "new_risks": array of {"text": short description, "quote": exact added sentence},\n'
    '  "removed_risks": array of {"text": short description, "quote": exact removed sentence},\n'
    '  "concentration_change": string or null (customer/supplier concentration change),\n'
    '  "capex_change": string or null (capital-expenditure plan change),\n'
    '  "guidance_tone_change": string or null (change in guidance/outlook tone),\n'
    '  "material_change_score": integer 1-5 (1 = boilerplate, 5 = highly material),\n'
    '  "summary": one sentence on the single most material change.\n'
    "Every array may be empty. Use null where the diff says nothing on a topic. "
    "List AT MOST the 8 most material new risks and the 8 most material removed "
    "risks, and keep every quote under 60 words, so the JSON stays complete. "
    "Never invent a quote: each quote must appear verbatim in the diff.\n\nDIFF:\n"
)


def assess_with_llm(
    text_generator: Any,
    entries: Sequence[Mapping[str, Any]],
    *,
    form: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """Prompt the model with ONLY the diff. Returns ``(parsed, record, error)``.

    ``record`` is the persisted ``{prompt, system, raw, model}`` so a reader can
    audit exactly what the model saw and said. The parsed result is evidence and
    is never used as a numeric forecast input.
    """
    payload = _diff_payload(entries)
    if not payload.strip():
        return None, None, f"{form}: no textual changes to assess"
    if text_generator is None:
        return None, None, f"{form}: no text generator configured"
    prompt = _LLM_INSTRUCTIONS + payload
    record: dict[str, Any] = {
        "form": form,
        "system": _LLM_SYSTEM,
        "prompt": prompt,
        "raw": None,
        "model": None,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    try:
        generated = text_generator.generate_text(
            system=_LLM_SYSTEM,
            prompt=prompt,
            max_tokens=3000,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001 - a model outage is data, not a crash
        record["raw"] = f"error: {exc}"
        return None, record, f"{form}: LLM call failed: {exc}"
    raw_text = getattr(generated, "text", "") or ""
    record["raw"] = raw_text
    record["model"] = getattr(generated, "model", None)
    parsed = _parse_json_object(raw_text)
    if parsed is None:
        return None, record, f"{form}: could not parse model JSON"
    return parsed, record, None


def _parse_json_object(text: str) -> dict[str, Any] | None:
    candidate = str(text or "").strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\n?", "", candidate)
        candidate = re.sub(r"\n?```$", "", candidate).strip()
    start = candidate.find("{")
    if start == -1:
        return None
    end = candidate.rfind("}")
    if end > start:
        try:
            parsed = json.loads(candidate[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    # Best-effort salvage of a response truncated mid-object (e.g. hit the token
    # cap): trim to the last complete "key": value pair and close the braces.
    return _salvage_truncated_object(candidate[start:])


def _salvage_truncated_object(fragment: str) -> dict[str, Any] | None:
    for cut in (fragment.rfind('"}'), fragment.rfind('"'), fragment.rfind("]")):
        if cut <= 0:
            continue
        head = fragment[: cut + (2 if fragment[cut : cut + 2] == '"}' else 1)]
        head = head.rstrip().rstrip(",")
        for suffix in ("}", "]}", '"}]}', '"}}'):
            try:
                parsed = json.loads(head + suffix)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def _apply_llm(entries: list[dict[str, Any]], parsed: Mapping[str, Any]) -> None:
    """Distribute the model's evidence onto the matching section entries."""
    material = parsed.get("material_change_score")
    material_int = material if isinstance(material, int) and 1 <= material <= 5 else None
    for entry in entries:
        entry["material_change_score"] = material_int
        label = entry["section"]
        if label == "Risk Factors":
            entry["new_risks"] = _clean_risk_list(parsed.get("new_risks"))
            entry["removed_risks"] = _clean_risk_list(parsed.get("removed_risks"))
        elif label == "Customer/Supplier Concentration":
            entry["concentration_change"] = _clean_str(parsed.get("concentration_change"))
        elif label == "Capex":
            entry["capex_change"] = _clean_str(parsed.get("capex_change"))
        elif label == "Guidance":
            entry["guidance_tone_change"] = _clean_str(parsed.get("guidance_tone_change"))


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_risk_list(value: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if isinstance(item, Mapping):
            text = _clean_str(item.get("text")) or _clean_str(item.get("risk")) or ""
            quote = _clean_str(item.get("quote")) or ""
        else:
            text = _clean_str(item) or ""
            quote = ""
        if text or quote:
            out.append({"text": text, "quote": quote})
    return out


# --------------------------------------------------------------------------- #
# News clustering.
# --------------------------------------------------------------------------- #
def _sentiment_label(score: float) -> str:
    if score >= 0.5:
        return "positive"
    if score <= -0.5:
        return "negative"
    return "neutral"


def _lexicon_sentiment(text: str) -> float:
    tokens = set(_tokens(text))
    low = str(text or "").lower()
    pos = sum(1 for w in _POSITIVE_WORDS if (" " in w and w in low) or w in tokens)
    neg = sum(1 for w in _NEGATIVE_WORDS if (" " in w and w in low) or w in tokens)
    if pos == 0 and neg == 0:
        return 0.0
    return float(pos - neg) / float(pos + neg)


def _event_type(text: str) -> str:
    low = str(text or "").lower()
    for pattern, label in _EVENT_TYPE_KEYWORDS:
        if re.search(pattern, low):
            return label
    return "general"


def _exposure_flags(text: str) -> list[str]:
    low = str(text or "").lower()
    flags: list[str] = []
    for pattern, flag in _EXPOSURE_FLAG_KEYWORDS:
        if re.search(pattern, low) and flag not in flags:
            flags.append(flag)
    return flags


def cluster_events(
    items: Sequence[Mapping[str, Any]],
    *,
    as_of: date,
    lookback_days: int = NEWS_LOOKBACK_DAYS,
    max_events: int = 25,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Cluster news items into dated events with sentiment and exposure flags.

    Items are filtered ``published <= t`` and to the trailing ``lookback_days``
    (point-in-time), deduplicated by headline token overlap so the same story from
    three wires becomes one event, then labelled by type and sentiment.
    """
    cutoff = as_of - timedelta(days=max(1, int(lookback_days)))
    scored: list[dict[str, Any]] = []
    for item in items:
        published = _parse_iso(item.get("published") or item.get("published_date"))
        if published is None or published > as_of or published < cutoff:
            continue
        title = str(item.get("title") or item.get("headline") or "").strip()
        if not title:
            continue
        scored.append(
            {
                "date": published.isoformat(),
                "title": title,
                "url": str(item.get("url") or ""),
                "summary": str(item.get("summary") or item.get("snippet") or ""),
                "provider": item.get("provider") or item.get("source"),
                "insight_sentiment": item.get("insight_sentiment"),
                "_tokens": set(_tokens(title)),
                "_date": published,
            }
        )
    scored.sort(key=lambda row: row["_date"], reverse=True)

    clusters: list[dict[str, Any]] = []
    for row in scored:
        placed = False
        for cluster in clusters:
            overlap = row["_tokens"] & cluster["_tokens"]
            union = row["_tokens"] | cluster["_tokens"]
            jaccard = (len(overlap) / len(union)) if union else 0.0
            within_window = abs((row["_date"] - cluster["_date"]).days) <= 3
            if jaccard >= 0.5 and within_window:
                cluster["_members"].append(row)
                cluster["_tokens"] = cluster["_tokens"] | row["_tokens"]
                if row["_date"] < cluster["_date"]:
                    cluster["_date"] = row["_date"]
                placed = True
                break
        if not placed:
            clusters.append(
                {
                    "_date": row["_date"],
                    "_tokens": set(row["_tokens"]),
                    "_members": [row],
                }
            )

    events: list[dict[str, Any]] = []
    for cluster in clusters:
        members = cluster["_members"]
        head = members[0]
        blob = " ".join(f"{m['title']} {m['summary']}" for m in members)
        insight = next(
            (m["insight_sentiment"] for m in members if m.get("insight_sentiment") is not None),
            None,
        )
        if isinstance(insight, str) and insight.strip():
            label = insight.strip().lower()
            sentiment = label if label in {"positive", "negative", "neutral"} else _sentiment_label(
                _lexicon_sentiment(blob)
            )
        else:
            sentiment = _sentiment_label(_lexicon_sentiment(blob))
        events.append(
            {
                "date": cluster["_date"].isoformat(),
                "type": _event_type(blob),
                "sentiment": sentiment,
                "headline": head["title"],
                "url": head["url"],
                "n_sources": len(members),
                "exposure_flags": _exposure_flags(blob),
                "sentiment_method": "massive_insight" if insight else "keyword_lexicon",
            }
        )
    events.sort(key=lambda e: e["date"], reverse=True)
    notes: list[str] = []
    if not events:
        notes.append("no company news in the point-in-time window")
    return events[: max(1, int(max_events))], notes


# --------------------------------------------------------------------------- #
# News fetch (Exa + Massive), point-in-time.
# --------------------------------------------------------------------------- #
def _fetch_news_items(
    *,
    ticker: str,
    company_name: str | None,
    exa_client: Any,
    market_client: Any,
    as_of: date,
    lookback_days: int,
    errors: list[dict[str, str]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    start = (as_of - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    end = as_of.strftime("%Y-%m-%d")
    name = (company_name or ticker).strip()

    if exa_client is not None:
        query = (
            f"{name} ({ticker}) news: earnings, guidance, products, management, "
            "analyst actions, regulation"
        )
        try:
            results = exa_client.search(
                query,
                num_results=12,
                start_published_date=f"{start}T00:00:00.000Z",
                end_published_date=f"{end}T23:59:59.000Z",
                category="news",
            )
        except Exception as exc:  # noqa: BLE001
            errors.append({"source": "text.news.exa", "error": str(exc)})
            results = []
        for result in results or []:
            items.append(
                {
                    "title": getattr(result, "title", "") or "",
                    "url": getattr(result, "url", "") or "",
                    "published": getattr(result, "published_date", None),
                    "summary": getattr(result, "snippet", "") or "",
                    "provider": "exa",
                }
            )

    if market_client is not None:
        try:
            payload = market_client.get_news(ticker, params={"limit": 40})
        except Exception as exc:  # noqa: BLE001
            errors.append({"source": "text.news.massive", "error": str(exc)})
            payload = None
        rows = (payload or {}).get("results") if isinstance(payload, Mapping) else None
        for row in rows or []:
            if not isinstance(row, Mapping):
                continue
            url = str(row.get("article_url") or "").strip()
            if not url:
                continue
            publisher = row.get("publisher")
            source = publisher.get("name") if isinstance(publisher, Mapping) else None
            items.append(
                {
                    "title": str(row.get("title") or ""),
                    "url": url,
                    "published": row.get("published_utc"),
                    "summary": str(row.get("description") or ""),
                    "provider": "massive",
                    "source": source,
                    "insight_sentiment": _massive_insight_sentiment(row, ticker),
                }
            )
    return items


def _massive_insight_sentiment(row: Mapping[str, Any], ticker: str) -> str | None:
    insights = row.get("insights")
    if not isinstance(insights, list):
        return None
    symbol = str(ticker).strip().upper()
    for insight in insights:
        if not isinstance(insight, Mapping):
            continue
        if str(insight.get("ticker") or "").strip().upper() == symbol:
            sentiment = insight.get("sentiment")
            return str(sentiment).strip().lower() if sentiment else None
    return None


# --------------------------------------------------------------------------- #
# Section assembly.
# --------------------------------------------------------------------------- #
def _fetch_filing_sections(
    sec_client: Any, filing: Mapping[str, Any]
) -> tuple[dict[str, str], str | None]:
    url = filing.get("url")
    if not isinstance(url, str) or not url:
        return {}, "filing had no document URL"
    try:
        document = sec_client.fetch_text(url)
    except Exception as exc:  # noqa: BLE001
        return {}, f"could not fetch {filing.get('form')} {filing.get('filing_date')}: {exc}"
    return extract_sections(document), None


def build_text_section(
    ticker: str,
    *,
    sec_client: Any = None,
    exa_client: Any = None,
    market_client: Any = None,
    text_generator: Any = None,
    company_name: str | None = None,
    as_of: date | str | None = None,
    lookback_days: int = NEWS_LOOKBACK_DAYS,
    wanted: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Engine entry point: build ``packet["text"]`` (SPEC 5.6).

    Every stage degrades independently — a filing that will not fetch, a model
    call that fails or a news source that is down each records a reason in
    ``errors`` and leaves the rest of the section intact. Raises
    :class:`TextError` only when *nothing* could be produced.
    """
    resolved = _resolve_as_of(as_of)
    errors: list[dict[str, str]] = []
    filing_changes: list[dict[str, Any]] = []
    prompts: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []

    # ------- filings + diffs -------
    if sec_client is not None:
        try:
            cik = sec_client.cik_for_ticker(ticker)
            submissions = sec_client.submissions(cik)
        except Exception as exc:  # noqa: BLE001
            errors.append({"source": "text.filings", "error": str(exc)})
            submissions = None
        if submissions is not None:
            selected = select_filings(submissions, as_of=resolved, wanted=wanted)
            for form, filings in selected.items():
                sections_by_filing: list[tuple[dict[str, Any], dict[str, str]]] = []
                for filing in filings:
                    parsed_sections, error = _fetch_filing_sections(sec_client, filing)
                    if error:
                        errors.append({"source": f"text.filing.{form}", "error": error})
                    if parsed_sections:
                        sections_by_filing.append((filing, parsed_sections))
                        sources.append(
                            {
                                "provider": "SEC EDGAR",
                                "url": filing.get("url"),
                                "note": f"{form} filed {filing.get('filing_date')}",
                                "fetched_at": datetime.now(UTC).isoformat(),
                            }
                        )
                # Diff each filing against the next-older comparable filing.
                for i in range(len(sections_by_filing) - 1):
                    current, current_sections = sections_by_filing[i]
                    prior, prior_sections = sections_by_filing[i + 1]
                    entries = _diff_pair(current, prior, current_sections, prior_sections)
                    # The newest comparison of each form gets the LLM assessment.
                    if i == 0:
                        parsed, record, llm_error = assess_with_llm(
                            text_generator, entries, form=form
                        )
                        if record is not None:
                            prompts.append(record)
                        if llm_error:
                            errors.append({"source": f"text.llm.{form}", "error": llm_error})
                        if parsed is not None:
                            _apply_llm(entries, parsed)
                    for entry in entries:
                        entry.pop("_added", None)
                        entry.pop("_removed", None)
                    filing_changes.extend(entries)
    else:
        errors.append({"source": "text.filings", "error": "no SEC client configured"})

    # ------- news -------
    news_items = _fetch_news_items(
        ticker=ticker,
        company_name=company_name,
        exa_client=exa_client,
        market_client=market_client,
        as_of=resolved,
        lookback_days=lookback_days,
        errors=errors,
    )
    events, event_notes = cluster_events(news_items, as_of=resolved, lookback_days=lookback_days)

    # Section-level exposure flags: the union across events and filing guidance.
    exposure_flags: list[str] = []
    for event in events:
        for flag in event.get("exposure_flags", []):
            if flag not in exposure_flags:
                exposure_flags.append(flag)

    if not filing_changes and not events:
        raise TextError(
            "; ".join(e["error"] for e in errors) or "no filings or news available"
        )

    return {
        "as_of": resolved.isoformat(),
        "filing_changes": filing_changes,
        "events": events,
        "exposure_flags": exposure_flags,
        "prompts": prompts,
        "sources": sources,
        "notes": event_notes,
        "version": MODULE_VERSION,
        "errors": errors,
    }
