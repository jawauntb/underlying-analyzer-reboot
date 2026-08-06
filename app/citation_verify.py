"""Citation verifier for Vision v2 analyst memos.

Vision v2 memos cite SEC EDGAR filings, XBRL company facts, the SEC
multi-quarter trend pack, an Earnings Source Pack (8-K sections + earnings
calendar), and an Exa web research pack. Each cited fact is rendered as a
parenthesized inline citation, e.g. ``(SEC XBRL Revenue, Q1 FY2027:
$137,237M)`` or ``(Exa: techcrunch.com, 2025-12-03)``.

Public entry points:

* :func:`extract_citations` — scan a memo for citation candidates.
* :func:`verify_citations` — run the full verification pipeline and return
  a :class:`CitationVerificationResult` with per-citation status and gauge
  percentages.

The verifier is intentionally tolerant of partial reports and exotic
citation text: exceptions degrade to ``uncheckable``, missing report
sections produce ``concept_missing``, unrecognized formats become
``uncheckable``. The verifier never raises.
"""

from __future__ import annotations

import re
import time
import traceback
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CitationCheck",
    "CitationVerificationResult",
    "classify_citation",
    "extract_citations",
    "verify_citations",
]


# ---------------------------------------------------------------------------
# Public data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CitationCheck:
    """One verified citation row.

    ``raw`` is the parenthesized text including parens. ``kind`` is one of
    ``sec_xbrl``, ``sec_filing``, ``sec_trend_pack``, ``sec_earnings_section``,
    ``earnings_calendar``, ``exa``, ``unknown``. ``status`` is one of
    ``verified``, ``value_mismatch``, ``concept_missing``, ``uncheckable``.
    ``position`` is the offset of the opening ``(`` in the memo.
    """

    raw: str
    kind: str
    target: str
    cited_value: str | None
    matched_value: str | None
    status: str
    note: str
    position: int


@dataclass(frozen=True)
class CitationVerificationResult:
    """Aggregate verification result for one memo."""

    total: int
    checkable: int
    verified: int
    value_mismatch: int
    concept_missing: int
    uncheckable: int
    percent_verified: float
    checks: list[CitationCheck] = field(default_factory=list)
    elapsed_ms: int = 0


# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------


STATUS_VERIFIED = "verified"
STATUS_VALUE_MISMATCH = "value_mismatch"
STATUS_CONCEPT_MISSING = "concept_missing"
STATUS_UNCHECKABLE = "uncheckable"


# ---------------------------------------------------------------------------
# Citation extraction
# ---------------------------------------------------------------------------


# Parenthesized chunks 4-300 chars, no nested parens.
_CITATION_RE = re.compile(r"\(([^()]{4,300})\)")

# Strong signals that mark a parenthesized chunk as a citation rather than
# narrative aside.
_STRONG_TOKENS: tuple[str, ...] = (
    "SEC",
    "XBRL",
    "Item",
    "Filed",
    "filed",
    "Exa:",
    "source",
    "Source",
    "8-K",
    "10-K",
    "10-Q",
    "Trend Pack",
    "Earnings Calendar",
    "FY",
    "YoY",
    "QoQ",
    "Provider",
    "Citation",
)

# Patterns that, when present *anywhere* inside a parenthesized chunk, count
# as evidence the chunk is a citation: a colon followed by a money value,
# date, percent, or comma-separated number.
_COLON_VALUE_RE = re.compile(
    r":\s*"
    r"(?:"
    r"\$[\d,]+(?:\.\d+)?[KMBkmb]?"  # $137,237M
    r"|\d{4}-\d{2}-\d{2}"  # 2026-05-20
    r"|\d+(?:\.\d+)?\s*%"  # 12.7%
    r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?"  # 50,344
    r")"
)

# Pattern for "Q[1-4]" (used as another strong signal) — common in Vision v2.
_QUARTER_RE = re.compile(r"\bQ[1-4]\b")

# A short list of obviously-non-citation phrases. We could rely on the
# token filter alone, but explicit denial keeps the heuristic robust to
# parenthesized prose that happens to include "Source" or "FY" as part of
# normal text.
_OBVIOUS_NEGATIVE_PREFIXES: tuple[str, ...] = (
    "see ",
    "see,",
    "see;",
    "see:",
    "i.e.",
    "i.e ",
    "e.g.",
    "e.g ",
    "approx",
    "roughly",
    "about ",
)


def _looks_like_citation(content: str) -> bool:
    """Return True if ``content`` (the inside of a parenthesized chunk)
    looks like a Vision v2 citation by either having a strong token or by
    containing a ``key: value`` pattern with a numeric/date payload."""

    text = content.strip()
    if len(text) < 4:
        return False

    lowered = text.lower()
    for prefix in _OBVIOUS_NEGATIVE_PREFIXES:
        if lowered.startswith(prefix):
            return False

    for token in _STRONG_TOKENS:
        if token in text:
            return True

    if _COLON_VALUE_RE.search(text):
        return True

    if _QUARTER_RE.search(text):
        return True

    return False


def extract_citations(memo_text: str) -> list[tuple[str, int]]:
    """Find every parenthesized chunk in ``memo_text`` that looks like a
    citation and return ``[(raw, position), ...]``.

    ``raw`` includes the surrounding parens; ``position`` is the offset of
    the opening ``(``. The heuristic combines a strong-token allowlist
    (``SEC``, ``Exa:``, ``Item``, etc.) with a generic key:value pattern
    (a colon followed by a money/date/percent value) so that even custom
    citation formats are picked up.
    """

    if not isinstance(memo_text, str) or not memo_text:
        return []

    out: list[tuple[str, int]] = []
    for match in _CITATION_RE.finditer(memo_text):
        content = match.group(1)
        if _looks_like_citation(content):
            out.append((match.group(0), match.start()))
    return out


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


# All classification patterns operate on the *inside* of the parens (no
# surrounding parens). They are tried in order; the first match wins.

_RE_SEC_XBRL = re.compile(
    r"^SEC\s+XBRL\s+(?P<target>[A-Za-z][A-Za-z &/-]*?)"
    r"(?:,\s*(?P<period>[^:]+?))?"
    r"\s*:\s*(?P<value>.+?)\s*$"
)

_RE_SEC_FILING = re.compile(
    r"^SEC\s+(?P<form>10-K|10-Q|8-K)\s+"
    r"(?P<item>Item\s+[\d.A-Za-z]+)"
    r"(?:\s+(?P<heading>[^,]+?))?"
    r",\s*filed\s+(?P<date>\d{4}-\d{2}-\d{2})\s*$"
)

_RE_SEC_TREND_PACK = re.compile(
    r"^SEC\s+Trend\s+Pack\s*:\s*(?P<target>[^=]+?)\s*=\s*(?P<value>.+?)\s*$"
)

_RE_SEC_EARNINGS_SECTION = re.compile(
    r"^SEC\s+8-K\s+(?P<item>Item\s+[\d.]+)"
    r"(?:\s+(?P<heading>[^,]+?))?"
    r"(?:,\s*filed\s+(?P<date>\d{4}-\d{2}-\d{2}))?\s*$"
)

_RE_EARNINGS_CALENDAR = re.compile(
    r"^Earnings\s+Calendar(?:,\s*(?P<note>.+?))?\s*$"
)

_RE_EXA = re.compile(
    r"^Exa\s*:\s*(?P<domain>[\w.\-]+)"
    r"(?:,\s*(?P<date>\d{4}-\d{2}-\d{2}))?"
    r"(?:,\s*(?P<extra>.+?))?\s*$"
)


def _strip_parens(raw: str) -> str:
    text = raw.strip()
    if text.startswith("(") and text.endswith(")"):
        return text[1:-1].strip()
    return text


def classify_citation(raw: str) -> dict[str, Any]:
    """Classify a citation string. Returns a dict with at minimum a
    ``kind`` key plus any extracted named groups. ``kind`` is one of
    ``sec_xbrl``, ``sec_filing``, ``sec_trend_pack``, ``sec_earnings_section``,
    ``earnings_calendar``, ``exa``, or ``unknown``.
    """

    body = _strip_parens(raw)

    # SEC 8-K earnings sections must be classified *before* the generic
    # filing pattern, otherwise the filing pattern would swallow them.
    if body.startswith("SEC 8-K"):
        m = _RE_SEC_EARNINGS_SECTION.match(body)
        if m:
            return {"kind": "sec_earnings_section", **m.groupdict()}

    m = _RE_SEC_XBRL.match(body)
    if m:
        return {"kind": "sec_xbrl", **m.groupdict()}

    m = _RE_SEC_FILING.match(body)
    if m:
        return {"kind": "sec_filing", **m.groupdict()}

    m = _RE_SEC_TREND_PACK.match(body)
    if m:
        return {"kind": "sec_trend_pack", **m.groupdict()}

    m = _RE_EARNINGS_CALENDAR.match(body)
    if m:
        return {"kind": "earnings_calendar", **m.groupdict()}

    m = _RE_EXA.match(body)
    if m:
        return {"kind": "exa", **m.groupdict()}

    return {"kind": "unknown"}


# ---------------------------------------------------------------------------
# Value normalization
# ---------------------------------------------------------------------------


# Suffix multipliers for human-formatted money / counts. Lowercase keys.
_MAGNITUDE_SUFFIXES: dict[str, float] = {
    "k": 1e3,
    "m": 1e6,
    "mm": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "t": 1e12,
    "tn": 1e12,
}


_MONEY_RE = re.compile(
    r"^\s*(?P<sign>-?)\$?\s*(?P<num>[\d,]+(?:\.\d+)?)\s*"
    r"(?P<suffix>[KkMmBbTt]{1,2})?\s*$"
)


def _normalize_money(s: str) -> float | None:
    """Parse a money string into a float in base units (dollars).

    Accepts forms like ``$137,237M``, ``137237000000``, ``-$1.2B``, ``50,344``.
    Returns ``None`` if the string can't be parsed.
    """

    if not isinstance(s, str):
        return None
    cleaned = s.strip().replace("$", "").replace(",", "")
    if not cleaned:
        return None
    m = _MONEY_RE.match(s.strip())
    if not m:
        # Fall back: try plain float.
        try:
            return float(cleaned)
        except ValueError:
            return None
    sign = -1.0 if m.group("sign") == "-" else 1.0
    try:
        num = float(m.group("num").replace(",", ""))
    except ValueError:
        return None
    suffix = (m.group("suffix") or "").lower()
    multiplier = _MAGNITUDE_SUFFIXES.get(suffix, 1.0) if suffix else 1.0
    return sign * num * multiplier


_PERCENT_RE = re.compile(r"^\s*(?P<num>-?\d+(?:\.\d+)?)\s*%\s*$")


def _normalize_percent(s: str) -> float | None:
    """Parse a percent string (``"12.7%"``) into a float (``0.127``)."""

    if not isinstance(s, str):
        return None
    m = _PERCENT_RE.match(s)
    if not m:
        return None
    try:
        return float(m.group("num")) / 100.0
    except ValueError:
        return None


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _normalize_date(s: str) -> str | None:
    if not isinstance(s, str):
        return None
    text = s.strip()
    if _DATE_RE.match(text):
        return text
    return None


def _normalize_value(s: Any) -> float | str | None:
    """Best-effort normalization. Tries money → percent → float → string.

    Returns ``None`` when ``s`` is ``None``. Strings that don't parse as a
    number fall through to a trimmed string. Numbers come back as floats.
    """

    if s is None:
        return None
    if isinstance(s, bool):
        # bool is a subclass of int — bail out explicitly so we don't
        # mis-parse True/False as 1.0/0.0.
        return str(s)
    if isinstance(s, (int, float)):
        return float(s)
    if not isinstance(s, str):
        return str(s)

    text = s.strip()
    if not text:
        return None

    if "%" in text:
        pct = _normalize_percent(text)
        if pct is not None:
            return pct

    money = _normalize_money(text)
    if money is not None:
        return money

    try:
        return float(text.replace(",", ""))
    except ValueError:
        pass

    return text


def _values_match(
    a: Any,
    b: Any,
    *,
    tolerance: float = 0.005,
) -> bool:
    """Compare two normalized values. Numeric values match within a
    relative tolerance (default 0.5%). Strings match case-insensitively
    after trimming. Mixed numeric/string returns ``False``."""

    if a is None or b is None:
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        af = float(a)
        bf = float(b)
        if af == bf:
            return True
        scale = max(abs(af), abs(bf), 1.0)
        return abs(af - bf) / scale <= tolerance
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().casefold() == b.strip().casefold()
    return False


def _format_value(v: Any) -> str:
    """Compact human-friendly value formatting for ``matched_value``
    strings carried in the result. Floats use up to 6 significant digits."""

    if v is None:
        return ""
    if isinstance(v, float):
        if v != v:  # NaN
            return "nan"
        # Large numbers: switch to scientific only for very big values.
        if abs(v) >= 1e12:
            return f"{v:.3e}"
        # Avoid trailing-zero noise.
        formatted = f"{v:.6g}"
        return formatted
    return str(v)


# ---------------------------------------------------------------------------
# Report walking helpers
# ---------------------------------------------------------------------------


def _safe_get(obj: Any, key: str) -> Any:
    if isinstance(obj, Mapping):
        if key in obj:
            return obj[key]
        # Case-insensitive fallback for dict keys.
        lowered = key.casefold()
        for k, v in obj.items():
            if isinstance(k, str) and k.casefold() == lowered:
                return v
    return None


_PATH_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def _walk_path(root: Any, path: str) -> Any:
    """Walk a dotted/index path like ``Quarters[0].cash_from_operations``
    against ``root``. Returns the value found or ``None``."""

    if root is None:
        return None
    cursor: Any = root
    for token in _PATH_TOKEN_RE.finditer(path):
        key = token.group(1)
        idx = token.group(2)
        if key is not None:
            key = key.strip()
            if not isinstance(cursor, Mapping):
                return None
            if key in cursor:
                cursor = cursor[key]
                continue
            # Case-insensitive fallback.
            lowered = key.casefold()
            found = False
            for k, v in cursor.items():
                if isinstance(k, str) and k.casefold() == lowered:
                    cursor = v
                    found = True
                    break
            if not found:
                return None
        elif idx is not None:
            try:
                i = int(idx)
            except ValueError:
                return None
            if not isinstance(cursor, list):
                return None
            if i < 0 or i >= len(cursor):
                return None
            cursor = cursor[i]
    return cursor


# Concept-name normalization for SEC XBRL lookups. The memo cites
# things like "Revenue", "Cash", "Long Term Debt"; the report stores
# "Revenue", "Cash And Equivalents", "Long Term Debt". We map common
# aliases so a "Cash" citation finds "Cash And Equivalents".
_XBRL_CONCEPT_ALIASES: dict[str, tuple[str, ...]] = {
    "cash": ("Cash And Equivalents", "Cash"),
    "cash and equivalents": ("Cash And Equivalents",),
    "revenue": ("Revenue", "Revenues"),
    "revenues": ("Revenue", "Revenues"),
    "net income": ("Net Income",),
    "operating income": ("Operating Income",),
    "assets": ("Assets", "Total Assets"),
    "liabilities": ("Liabilities", "Total Liabilities"),
    "equity": ("Stockholders Equity", "Total Equity"),
    "stockholders equity": ("Stockholders Equity",),
    "long term debt": ("Long Term Debt",),
    "diluted eps": ("Diluted EPS",),
    "shares outstanding": ("Shares Outstanding",),
    "inventory": ("Inventory",),
}


def _xbrl_lookup_candidates(target: str) -> tuple[str, ...]:
    key = target.strip().casefold()
    aliases = _XBRL_CONCEPT_ALIASES.get(key)
    if aliases:
        return aliases
    # Return the original (title-cased) target as a single guess.
    return (target.strip(),)


# ---------------------------------------------------------------------------
# Per-kind checkers
# ---------------------------------------------------------------------------


def _check_sec_xbrl(
    info: Mapping[str, Any],
    report: Mapping[str, Any] | None,
) -> tuple[str, str | None, str]:
    """Verify a ``SEC XBRL <concept>, <period>: <value>`` citation.

    Returns ``(status, matched_value_str, note)``.
    """

    target = (info.get("target") or "").strip()
    cited_value = info.get("value")
    if not target or cited_value is None:
        return STATUS_UNCHECKABLE, None, "could not parse SEC XBRL citation body"

    if not isinstance(report, Mapping):
        return STATUS_CONCEPT_MISSING, None, "no report supplied"

    candidates = _xbrl_lookup_candidates(target)

    # Search Company Facts in SEC Source Pack first — that's the canonical
    # XBRL store. Fall back to Trend Pack Metrics if present.
    sec_pack = _safe_get(report, "SEC Source Pack")
    facts = _safe_get(sec_pack, "Company Facts") if sec_pack else None

    found_value: Any = None
    found_concept: str | None = None
    if isinstance(facts, Mapping):
        for cand in candidates:
            fact = _safe_get(facts, cand)
            if isinstance(fact, Mapping):
                value = fact.get("Value")
                if value is None:
                    value = fact.get("value")
                if value is not None:
                    found_value = value
                    found_concept = cand
                    break

    # Trend pack Metrics fallback: latest value of the matching series.
    if found_value is None:
        trend_pack = _safe_get(report, "SEC Trend Pack")
        metrics = _safe_get(trend_pack, "Metrics") if trend_pack else None
        if isinstance(metrics, Mapping):
            for cand in candidates:
                series = _safe_get(metrics, cand)
                if isinstance(series, Mapping):
                    values = series.get("values")
                    if isinstance(values, list) and values:
                        last = values[-1]
                        # values are [(period, value), ...] tuples or lists.
                        if isinstance(last, (list, tuple)) and len(last) >= 2:
                            found_value = last[1]
                        else:
                            found_value = last
                        found_concept = cand
                        break

    if found_value is None:
        return (
            STATUS_CONCEPT_MISSING,
            None,
            f"no XBRL fact named {target!r} in report",
        )

    cited_norm = _normalize_value(cited_value)
    found_norm = _normalize_value(found_value)
    matched_str = _format_value(found_norm)

    if _values_match(cited_norm, found_norm):
        return (
            STATUS_VERIFIED,
            matched_str,
            f"matched {found_concept} = {matched_str}",
        )
    return (
        STATUS_VALUE_MISMATCH,
        matched_str,
        f"cited {cited_value!r} but report has {matched_str}",
    )


def _iter_citations(report: Mapping[str, Any] | None) -> Iterable[Mapping[str, Any]]:
    if not isinstance(report, Mapping):
        return ()
    sec_pack = _safe_get(report, "SEC Source Pack")
    cites = _safe_get(sec_pack, "Citations") if sec_pack else None
    if isinstance(cites, list):
        for c in cites:
            if isinstance(c, Mapping):
                yield c


def _check_sec_filing(
    info: Mapping[str, Any],
    report: Mapping[str, Any] | None,
) -> tuple[str, str | None, str]:
    form = (info.get("form") or "").strip()
    item = (info.get("item") or "").strip()
    date = (info.get("date") or "").strip()
    if not form or not item:
        return STATUS_UNCHECKABLE, None, "could not parse SEC filing citation"

    if not isinstance(report, Mapping):
        return STATUS_CONCEPT_MISSING, None, "no report supplied"

    for c in _iter_citations(report):
        c_form = str(c.get("Form") or "").strip()
        c_item = str(c.get("Item") or "").strip()
        # The citation entries use a "Label" like
        # "SEC 10-K Item 1 Business" — Item is sometimes only in Label.
        label = str(c.get("Label") or "")
        c_filed = str(c.get("Filing Date") or c.get("Filed") or "").strip()
        item_match = (
            c_item.casefold() == item.casefold()
            or item.casefold() in label.casefold()
        )
        if c_form.casefold() == form.casefold() and item_match:
            if date and c_filed and c_filed != date:
                return (
                    STATUS_VALUE_MISMATCH,
                    c_filed,
                    f"cited filed date {date} but report has {c_filed}",
                )
            return (
                STATUS_VERIFIED,
                c_filed or date,
                f"matched {form} {item}" + (f" filed {c_filed}" if c_filed else ""),
            )

    return (
        STATUS_CONCEPT_MISSING,
        None,
        f"no {form} {item} citation in SEC Source Pack",
    )


def _check_sec_trend_pack(
    info: Mapping[str, Any],
    report: Mapping[str, Any] | None,
) -> tuple[str, str | None, str]:
    target = (info.get("target") or "").strip()
    cited_value = info.get("value")
    if not target or cited_value is None:
        return STATUS_UNCHECKABLE, None, "could not parse trend pack citation"

    if not isinstance(report, Mapping):
        return STATUS_CONCEPT_MISSING, None, "no report supplied"

    trend_pack = _safe_get(report, "SEC Trend Pack")
    if not isinstance(trend_pack, Mapping):
        return STATUS_CONCEPT_MISSING, None, "no SEC Trend Pack in report"

    found = _walk_path(trend_pack, target)
    if found is None:
        return (
            STATUS_CONCEPT_MISSING,
            None,
            f"path {target!r} not found in SEC Trend Pack",
        )

    cited_norm = _normalize_value(cited_value)
    found_norm = _normalize_value(found)
    matched_str = _format_value(found_norm)
    if _values_match(cited_norm, found_norm):
        return STATUS_VERIFIED, matched_str, f"matched {target} = {matched_str}"
    return (
        STATUS_VALUE_MISMATCH,
        matched_str,
        f"cited {cited_value!r} but trend pack has {matched_str}",
    )


def _check_sec_earnings_section(
    info: Mapping[str, Any],
    report: Mapping[str, Any] | None,
) -> tuple[str, str | None, str]:
    item = (info.get("item") or "").strip()
    date = (info.get("date") or "").strip()

    if not isinstance(report, Mapping):
        return STATUS_CONCEPT_MISSING, None, "no report supplied"

    earnings_pack = _safe_get(report, "Earnings Source Pack")
    sections = _safe_get(earnings_pack, "SEC 8-K Sections") if earnings_pack else None

    if isinstance(sections, Mapping):
        for label, section in sections.items():
            if not isinstance(section, Mapping):
                continue
            s_item = str(section.get("Item") or "").strip()
            s_filed = str(
                section.get("Filing Date") or section.get("Filed") or ""
            ).strip()
            label_str = str(label or "")
            item_match = (
                s_item.casefold() == item.casefold()
                or item.casefold() in label_str.casefold()
            )
            if item_match:
                if date and s_filed and s_filed != date:
                    return (
                        STATUS_VALUE_MISMATCH,
                        s_filed,
                        f"cited filed date {date} but earnings pack has {s_filed}",
                    )
                return (
                    STATUS_VERIFIED,
                    s_filed or date or label_str,
                    f"matched 8-K {item}" + (f" filed {s_filed}" if s_filed else ""),
                )

    # Fall back to the SEC Source Pack earnings section list (some reports
    # only populate that path).
    sec_pack = _safe_get(report, "SEC Source Pack")
    earnings_sections = (
        _safe_get(sec_pack, "Earnings Sections") if sec_pack else None
    )
    if isinstance(earnings_sections, Mapping):
        for label, section in earnings_sections.items():
            if not isinstance(section, Mapping):
                continue
            s_item = str(section.get("Item") or "").strip()
            label_str = str(label or "")
            if (
                s_item.casefold() == item.casefold()
                or item.casefold() in label_str.casefold()
            ):
                return STATUS_VERIFIED, label_str, f"matched 8-K {item}"

    return STATUS_CONCEPT_MISSING, None, f"no 8-K {item} section in report"


def _check_earnings_calendar(
    info: Mapping[str, Any],
    report: Mapping[str, Any] | None,
) -> tuple[str, str | None, str]:
    if not isinstance(report, Mapping):
        return STATUS_CONCEPT_MISSING, None, "no report supplied"

    earnings_pack = _safe_get(report, "Earnings Source Pack")
    calendar = _safe_get(earnings_pack, "Calendar") if earnings_pack else None
    if isinstance(calendar, Mapping) and any(v is not None for v in calendar.values()):
        return STATUS_VERIFIED, "present", "earnings calendar present in report"
    return STATUS_CONCEPT_MISSING, None, "no earnings calendar in report"


def _domain_matches(citation_url: str, domain: str) -> bool:
    url = citation_url.casefold()
    dom = domain.strip().casefold()
    if not dom:
        return False
    return dom in url


def _check_exa(
    info: Mapping[str, Any],
    report: Mapping[str, Any] | None,
) -> tuple[str, str | None, str]:
    domain = (info.get("domain") or "").strip()
    date = (info.get("date") or "").strip()
    if not domain:
        return STATUS_UNCHECKABLE, None, "could not parse Exa citation domain"

    if not isinstance(report, Mapping):
        return STATUS_CONCEPT_MISSING, None, "no report supplied"

    exa_pack = _safe_get(report, "Exa Research Pack")
    cites = _safe_get(exa_pack, "Citations") if exa_pack else None
    if not isinstance(cites, list):
        return STATUS_CONCEPT_MISSING, None, "no Exa Research Pack citations in report"

    domain_hits: list[Mapping[str, Any]] = []
    for c in cites:
        if not isinstance(c, Mapping):
            continue
        url = str(c.get("url") or "")
        if _domain_matches(url, domain):
            domain_hits.append(c)

    if not domain_hits:
        return STATUS_CONCEPT_MISSING, None, f"no Exa citation matching {domain!r}"

    if date:
        for c in domain_hits:
            pub = str(c.get("published_date") or "").strip()
            # Exa published dates can be ISO timestamps; compare prefixes.
            if pub.startswith(date):
                return (
                    STATUS_VERIFIED,
                    pub or date,
                    f"matched Exa citation {domain} dated {pub}",
                )
        return (
            STATUS_VALUE_MISMATCH,
            str(domain_hits[0].get("published_date") or ""),
            f"cited Exa date {date} not found for {domain}",
        )

    matched = domain_hits[0]
    return (
        STATUS_VERIFIED,
        str(matched.get("url") or domain),
        f"matched Exa citation {domain}",
    )


# ---------------------------------------------------------------------------
# Dispatcher + public verifier
# ---------------------------------------------------------------------------


_CHECKERS = {
    "sec_xbrl": _check_sec_xbrl,
    "sec_filing": _check_sec_filing,
    "sec_trend_pack": _check_sec_trend_pack,
    "sec_earnings_section": _check_sec_earnings_section,
    "earnings_calendar": _check_earnings_calendar,
    "exa": _check_exa,
}


def _derive_target(kind: str, info: Mapping[str, Any]) -> str:
    """Extract a short human-readable target for the check row."""

    if kind == "sec_xbrl":
        return (info.get("target") or "").strip()
    if kind == "sec_filing":
        form = (info.get("form") or "").strip()
        item = (info.get("item") or "").strip()
        heading = (info.get("heading") or "").strip()
        parts = [p for p in (form, item, heading) if p]
        return " ".join(parts)
    if kind == "sec_trend_pack":
        return (info.get("target") or "").strip()
    if kind == "sec_earnings_section":
        item = (info.get("item") or "").strip()
        heading = (info.get("heading") or "").strip()
        return f"8-K {item} {heading}".strip()
    if kind == "earnings_calendar":
        return "Earnings Calendar"
    if kind == "exa":
        return (info.get("domain") or "").strip()
    return ""


def _derive_cited_value(kind: str, info: Mapping[str, Any]) -> str | None:
    if kind in {"sec_xbrl", "sec_trend_pack"}:
        v = info.get("value")
        return None if v is None else str(v).strip()
    if kind == "sec_filing":
        d = info.get("date")
        return None if d is None else str(d).strip()
    if kind == "sec_earnings_section":
        d = info.get("date")
        return None if d is None else str(d).strip()
    if kind == "exa":
        d = info.get("date")
        return None if d is None else str(d).strip()
    return None


def verify_citations(
    memo_text: str,
    *,
    report: dict[str, Any] | None,
) -> CitationVerificationResult:
    """Run the full Vision v2 citation verification pipeline.

    Walks each citation extracted from ``memo_text``, dispatches to the
    per-kind checker, and aggregates the result. Tolerant of partial or
    missing ``report`` data; never raises.
    """

    started = time.monotonic_ns()
    checks: list[CitationCheck] = []

    citations = extract_citations(memo_text or "")
    for raw, position in citations:
        try:
            info = classify_citation(raw)
            kind = info.get("kind", "unknown")
            target = _derive_target(kind, info)
            cited_value = _derive_cited_value(kind, info)
            checker = _CHECKERS.get(kind)
            if checker is None:
                status = STATUS_UNCHECKABLE
                matched = None
                note = f"unknown citation kind ({kind})"
            else:
                status, matched, note = checker(info, report)
        except Exception as exc:  # never raise — degrade to uncheckable
            status = STATUS_UNCHECKABLE
            matched = None
            note = f"verification error: {type(exc).__name__}: {exc}"
            kind = "unknown"
            target = ""
            cited_value = None
            # Optional: stash traceback in note only when debugging.
            _ = traceback  # keep import alive for type-checkers

        checks.append(
            CitationCheck(
                raw=raw,
                kind=kind,
                target=target,
                cited_value=cited_value,
                matched_value=matched,
                status=status,
                note=note,
                position=position,
            )
        )

    total = len(checks)
    # Single pass over checks instead of four separate generator scans.
    verified = value_mismatch = concept_missing = uncheckable = 0
    for c in checks:
        status = c.status
        if status == STATUS_VERIFIED:
            verified += 1
        elif status == STATUS_VALUE_MISMATCH:
            value_mismatch += 1
        elif status == STATUS_CONCEPT_MISSING:
            concept_missing += 1
        elif status == STATUS_UNCHECKABLE:
            uncheckable += 1
    checkable = total - uncheckable
    if checkable <= 0:
        # Document choice: when there's nothing to check, we report 1.0
        # (i.e. "no failures") so the UI gauge doesn't paint red on a memo
        # that simply happened to be uncheckable.
        percent_verified = 1.0
    else:
        percent_verified = verified / checkable

    elapsed_ms = max(0, (time.monotonic_ns() - started) // 1_000_000)

    return CitationVerificationResult(
        total=total,
        checkable=checkable,
        verified=verified,
        value_mismatch=value_mismatch,
        concept_missing=concept_missing,
        uncheckable=uncheckable,
        percent_verified=percent_verified,
        checks=checks,
        elapsed_ms=int(elapsed_ms),
    )
