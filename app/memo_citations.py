"""Additive citation-type annotations for Prism / Situate memo responses.

Both engines expose ``packet["memo"]["citations"]`` as a list of
``{"id", "claim", "source", "url", ...}`` rows. This module attaches one
optional, additive field per row without touching anything else::

    "citation_type": {"type": "<kind>", "source": "regex" | "jev", "confidence": <0..1 | null>}

``type`` is one of the kinds :mod:`app.citation_verify` already knows
(``sec_xbrl``, ``sec_filing``, ``sec_trend_pack``, ``sec_earnings_section``,
``earnings_calendar``, ``exa``). Rows that classify as ``unknown`` get NO
field, so a UI can simply render a badge when the field is present.

Classification is batched (one Jev request per memo) and cached by
:func:`app.citation_verify.classify_citations`; any failure leaves the packet
byte-for-byte as it was. The stored packet is never mutated - callers get a
copied packet/memo/citation list.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.citation_verify import classify_citations

CITATION_TYPE_FIELD = "citation_type"


def citation_text(row: Mapping[str, Any]) -> str:
    """The string we classify for one structured memo citation row.

    Mirrors how the memo renders the row (``source`` then ``claim``, with the
    URL when present) so the regex ladder and Jev see the same text a reader
    would.
    """
    source = str(row.get("source") or "").strip()
    claim = str(row.get("claim") or "").strip()
    url = str(row.get("url") or "").strip()
    head = f"{source}: {claim}" if source and claim else source or claim
    return f"({head}, {url})" if url else f"({head})"


def annotate_citations(
    citations: Any, *, jev_client: Any | None = None
) -> list[dict[str, Any]]:
    """Return copies of ``citations`` with ``citation_type`` where the kind is known."""
    if not isinstance(citations, list):
        return []
    rows = [row for row in citations if isinstance(row, Mapping)]
    if not rows:
        return [dict(row) if isinstance(row, Mapping) else row for row in citations]

    texts = [citation_text(row) for row in rows]
    verdicts = classify_citations(texts, jev_client=jev_client)
    by_row = dict(zip((id(row) for row in rows), verdicts, strict=True))

    out: list[dict[str, Any]] = []
    for row in citations:
        if not isinstance(row, Mapping):
            out.append(row)
            continue
        verdict = by_row.get(id(row))
        copied = dict(row)
        if verdict is not None and verdict.get("type") not in (None, "unknown"):
            copied[CITATION_TYPE_FIELD] = {
                "type": verdict["type"],
                "source": verdict["source"],
                "confidence": verdict.get("confidence"),
            }
        out.append(copied)
    return out


def annotate_packet_citations(
    packet: Any, *, jev_client: Any | None = None
) -> Any:
    """Return ``packet`` with memo citations annotated, or unchanged on any failure.

    Fail-open by construction: a missing memo, an unexpected shape, or any
    exception from classification returns the original object untouched.
    """
    if not isinstance(packet, dict):
        return packet
    memo = packet.get("memo")
    if not isinstance(memo, dict) or not isinstance(memo.get("citations"), list):
        return packet
    try:
        annotated = annotate_citations(memo["citations"], jev_client=jev_client)
    except Exception:  # noqa: BLE001 - annotation is an optimization, never a dependency
        return packet
    return {**packet, "memo": {**memo, "citations": annotated}}
