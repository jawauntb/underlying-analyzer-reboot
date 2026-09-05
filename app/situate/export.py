"""Export a built Situate packet as Markdown, JSON, or a typeset PDF.

The PDF is rendered through :mod:`app.memo_pdf` (reused, not rebuilt). Situate
never prints a point price target, so the payload's target fields are left empty
and the cheap/rich zones travel as prose and scenario rows instead.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

FORMATS = ("md", "json", "pdf")
CONTENT_TYPES = {
    "md": "text/markdown; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "pdf": "application/pdf",
}
_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class SituateExportError(ValueError):
    """Raised for an unknown export format."""


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    import math

    return number if math.isfinite(number) else None


def _pct(value: Any, *, digits: int = 1, sign: bool = True) -> str:
    number = _finite(value)
    if number is None:
        return "n/a"
    return f"{number * 100:{'+' if sign else ''}.{digits}f}%"


def _section(packet: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = packet.get(name)
    return value if isinstance(value, Mapping) else {}


def _mget(node: Any, key: str) -> dict[str, Any]:
    """The child mapping at ``key`` as a plain dict (or ``{}``)."""
    value = node.get(key) if isinstance(node, Mapping) else None
    return dict(value) if isinstance(value, Mapping) else {}


def to_json(packet: Mapping[str, Any], *, indent: int | None = 2) -> str:
    return json.dumps(packet, ensure_ascii=False, indent=indent, default=str)


def to_markdown(packet: Mapping[str, Any]) -> str:
    """The memo markdown, or a rebuilt deterministic memo when it is missing."""
    memo = _section(packet, "memo")
    text = str(memo.get("text") or "")
    if text.strip():
        return text
    # No memo section stored: rebuild deterministically so the export is never empty.
    from app.situate.memo import build_citations, derive_posture, render_markdown

    posture = derive_posture(packet)
    return render_markdown(packet, posture=posture, citations=build_citations(packet))


_MEMO_HEADING = re.compile(r"^#{1,3}\s+(.+?)\s*$", re.MULTILINE)


def _memo_sections(text: str) -> dict[str, str]:
    """Split the memo markdown into ``{heading: body}`` for the PDF template."""
    matches = list(_MEMO_HEADING.finditer(text or ""))
    if not matches:
        return {}
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip()
        if body:
            sections[title] = body
    return sections


def _pdf_scenarios(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    scenarios = _section(packet, "scenarios")
    rows: list[dict[str, Any]] = []
    for name in ("bear", "neutral", "bull"):
        block = _mget(scenarios, name)
        if not block:
            continue
        horizons = _mget(block, "horizons")
        twelve = _mget(horizons, "12")
        six = _mget(horizons, "6")
        three = _mget(horizons, "3")
        rows.append(
            {
                "name": name.title(),
                "rev_growth": _pct((three or {}).get("quantile")),
                "gm": _pct((six or {}).get("quantile")),
                "eps": _pct((twelve or {}).get("quantile")),
                "multiple": str(block.get("quantile_key") or "-"),
                "price": "-",
                "notes": str(block.get("state") or ""),
            }
        )
    return rows


def _pdf_citations(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    memo = _section(packet, "memo")
    citations = memo.get("citations")
    if not isinstance(citations, list):
        return []
    return [
        {
            "label": str(row.get("claim") or row.get("id") or ""),
            "source": str(row.get("source") or ""),
            "url": str(row.get("url") or ""),
        }
        for row in citations
        if isinstance(row, Mapping)
    ]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return []
    return [str(item) for item in value if str(item).strip()]


def to_pdf(packet: Mapping[str, Any]) -> bytes:
    """Render the packet's memo to PDF bytes via :mod:`app.memo_pdf`."""
    from app.memo_pdf import MemoPdfPayload, render_memo_pdf

    memo = _section(packet, "memo")
    profile = _section(packet, "profile")
    levels = _section(packet, "levels")
    posture = memo.get("posture") if isinstance(memo.get("posture"), Mapping) else {}

    text = to_markdown(packet)
    stance = str((posture or {}).get("stance") or "balanced").replace("_", " ").upper()

    payload = MemoPdfPayload(
        ticker=str(packet.get("ticker") or ""),
        company_name=str(profile.get("name") or ""),
        memo_text=text,
        memo_sections=_memo_sections(text) or None,
        document_title="SITUATE MEMO",
        recommendation=stance,
        sector=str(profile.get("sector") or "") or None,
        industry=str(profile.get("industry") or "") or None,
        generated_at=str(packet.get("generated_at") or ""),
        # Situate reports zones and distributions, never a point target: leave the
        # PDF's target fields empty and let the memo prose carry the zones.
        current_price=_finite(levels.get("current_price")),
        market_cap=_finite(profile.get("market_cap")),
        scenarios=_pdf_scenarios(packet) or None,
        citations=_pdf_citations(packet) or None,
        catalysts=_string_list((memo or {}).get("whats_priced_in")) or None,
        kill_criteria=_string_list((memo or {}).get("falsifiers")) or None,
        diligence_gaps=[
            f"{row.get('source')}: {row.get('error') or row.get('reason')}"
            for row in ((packet.get("meta") or {}).get("errors") or [])
            if isinstance(row, Mapping)
        ]
        or None,
    )
    return render_memo_pdf(payload)


def export_packet(packet: Mapping[str, Any], fmt: str) -> tuple[bytes, str, str]:
    """Return ``(body, content_type, filename)`` for one export format."""
    normalized = str(fmt or "").strip().lower()
    if normalized == "markdown":
        normalized = "md"
    if normalized not in FORMATS:
        raise SituateExportError(f"format must be one of {', '.join(FORMATS)} (got '{fmt}')")
    ticker = _FILENAME_UNSAFE.sub("_", str(packet.get("ticker") or "packet").upper()) or "PACKET"
    as_of = _FILENAME_UNSAFE.sub("_", str(packet.get("as_of") or "latest")) or "latest"
    filename = f"situate-{ticker}-{as_of}.{normalized}"
    if normalized == "json":
        return to_json(packet).encode("utf-8"), CONTENT_TYPES["json"], filename
    if normalized == "md":
        return to_markdown(packet).encode("utf-8"), CONTENT_TYPES["md"], filename
    return to_pdf(packet), CONTENT_TYPES["pdf"], filename
