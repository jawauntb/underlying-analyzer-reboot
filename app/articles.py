"""Research article artifacts: normalization and markdown rendering.

The agent publishes a summary article through ``compose_research_article``.
This module is the one place that decides what a valid article looks like, so
the same shape is produced whether the call came from the chat agent, the MCP
endpoint, or a direct HTTP client.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

STANCES = ("constructive", "neutral", "cautious", "avoid", "watch")
CONFIDENCE = ("low", "medium", "high")
SOURCE_KINDS = ("tool", "filing", "news", "chart", "other")

MAX_TITLE_CHARS = 140
MAX_SECTIONS = 12
MAX_RECOMMENDATIONS = 12
MAX_RISKS = 10
MAX_SOURCES = 24

DISCLAIMER = (
    "Research output from The Underlying Analyzer. Generated from market data, "
    "SEC filings, and public news for research purposes. Not investment advice."
)


class ArticleError(ValueError):
    """Raised when an article payload cannot be normalized."""


def normalize_article(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a composed research article."""
    if not isinstance(payload, dict):
        raise ArticleError("Article payload must be an object")

    title = _text(payload.get("title"), limit=MAX_TITLE_CHARS)
    if not title:
        raise ArticleError("Article requires a title")

    thesis = _text(payload.get("thesis"), limit=1200)
    if not thesis:
        raise ArticleError("Article requires a thesis")

    sections = _sections(payload.get("sections"))
    if not sections:
        raise ArticleError("Article requires at least one section")

    tickers = _tickers(payload.get("tickers"))
    recommendations = _recommendations(payload.get("recommendations"))

    return {
        "title": title,
        "subtitle": _text(payload.get("subtitle"), limit=240) or None,
        "thesis": thesis,
        "tickers": tickers,
        "sections": sections,
        "recommendations": recommendations,
        "risks": _string_list(payload.get("risks"), MAX_RISKS, 600),
        "sources": _sources(payload.get("sources")),
        "generated_at": datetime.now(UTC).isoformat(),
        "disclaimer": DISCLAIMER,
        "word_count": _word_count(thesis, sections),
    }


def article_markdown(article: dict[str, Any]) -> str:
    """Render a normalized article as portable markdown."""
    lines: list[str] = [f"# {article['title']}"]
    if article.get("subtitle"):
        lines.append(f"*{article['subtitle']}*")

    tickers = article.get("tickers") or []
    if tickers:
        lines.append("")
        lines.append(f"**Coverage:** {', '.join(tickers)}")

    lines.extend(["", "## Thesis", "", str(article.get("thesis") or "")])

    for section in article.get("sections") or []:
        lines.extend(["", f"## {section['heading']}", "", section["body"]])

    recommendations = article.get("recommendations") or []
    if recommendations:
        lines.extend(["", "## Recommendations", ""])
        lines.append("| Ticker | Stance | Action | Confidence |")
        lines.append("| --- | --- | --- | --- |")
        for item in recommendations:
            lines.append(
                "| {ticker} | {stance} | {action} | {confidence} |".format(
                    ticker=item.get("ticker") or "-",
                    stance=item.get("stance", ""),
                    action=_cell(item.get("action", "")),
                    confidence=item.get("confidence") or "-",
                )
            )
        for item in recommendations:
            rationale = item.get("rationale")
            invalidation = item.get("invalidation")
            if not rationale and not invalidation:
                continue
            label = item.get("ticker") or str(item.get("stance", "")).title()
            body = rationale or f"Invalidated if: {invalidation}"
            lines.extend(["", f"**{label}.** {body}"])
            if rationale and invalidation:
                lines.append(f"Invalidated if: {invalidation}")

    risks = article.get("risks") or []
    if risks:
        lines.extend(["", "## Risks", ""])
        lines.extend(f"- {risk}" for risk in risks)

    sources = article.get("sources") or []
    if sources:
        lines.extend(["", "## Sources", ""])
        for source in sources:
            if source.get("url"):
                lines.append(f"- [{source['label']}]({source['url']})")
            else:
                lines.append(f"- {source['label']}")

    lines.extend(["", "---", "", f"_{article.get('disclaimer', DISCLAIMER)}_"])
    return "\n".join(lines).strip() + "\n"


def article_summary(article: dict[str, Any]) -> str:
    """Short preview text used by the library list and save records."""
    thesis = str(article.get("thesis") or "")
    if len(thesis) <= 220:
        return thesis
    return thesis[:217].rstrip() + "..."


def _text(value: Any, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split()) if limit <= MAX_TITLE_CHARS else value.strip()
    if len(text) > limit:
        text = text[:limit].rstrip()
    return text


def _cell(value: str) -> str:
    return " ".join(str(value).split()).replace("|", "/")


def _tickers(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = [part.strip() for part in value.split(",")]
    elif isinstance(value, list):
        raw = [str(part).strip() for part in value]
    else:
        return []
    seen: list[str] = []
    for item in raw:
        symbol = item.upper()
        if symbol and symbol not in seen:
            seen.append(symbol)
    return seen[:12]


def _sections(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    sections: list[dict[str, str]] = []
    for item in value[:MAX_SECTIONS]:
        if not isinstance(item, dict):
            continue
        heading = _text(item.get("heading"), limit=120)
        body = _text(item.get("body"), limit=6000)
        if heading and body:
            sections.append({"heading": heading, "body": body})
    return sections


def _recommendations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    for entry in value[:MAX_RECOMMENDATIONS]:
        if not isinstance(entry, dict):
            continue
        action = _text(entry.get("action"), limit=400)
        if not action:
            continue
        stance = str(entry.get("stance") or "watch").strip().lower()
        confidence = str(entry.get("confidence") or "").strip().lower()
        ticker = _text(entry.get("ticker"), limit=12).upper() or None
        items.append(
            {
                "ticker": ticker,
                "stance": stance if stance in STANCES else "watch",
                "action": action,
                "rationale": _text(entry.get("rationale"), limit=900) or None,
                "invalidation": _text(entry.get("invalidation"), limit=600) or None,
                "confidence": confidence if confidence in CONFIDENCE else None,
            }
        )
    return items


def _string_list(value: Any, limit: int, chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    items = []
    for entry in value[:limit]:
        text = _text(entry, limit=chars)
        if text:
            items.append(text)
    return items


def _sources(value: Any) -> list[dict[str, str | None]]:
    if not isinstance(value, list):
        return []
    sources: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for entry in value[:MAX_SOURCES]:
        if isinstance(entry, str):
            entry = {"label": entry}
        if not isinstance(entry, dict):
            continue
        label = _text(entry.get("label"), limit=200)
        url = _text(entry.get("url"), limit=600) or None
        if not label:
            label = url or ""
        if not label:
            continue
        key = url or label
        if key in seen:
            continue
        seen.add(key)
        kind = str(entry.get("kind") or "other").strip().lower()
        if url and not url.startswith(("http://", "https://")):
            url = None
        sources.append(
            {
                "label": label,
                "url": url,
                "kind": kind if kind in SOURCE_KINDS else "other",
            }
        )
    return sources


def _word_count(thesis: str, sections: list[dict[str, str]]) -> int:
    words = len(thesis.split())
    for section in sections:
        words += len(section["body"].split())
    return words
