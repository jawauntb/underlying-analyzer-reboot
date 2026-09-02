"""Current news and policy context for one ticker, from Exa plus Massive.

Six curated Exa searches cover the buckets a memo has to have a view on —
the company itself, its industry, the regulation aimed at that industry, fiscal
and monetary policy, upcoming scheduled decisions, and the currency backdrop —
and Massive's ``/v2/reference/news`` supplies publisher-attributed company news
as an independent second source.

Every query is recorded in ``query_log`` with its result count and any error, so
a thin news section is legible as "the search returned nothing" rather than as
"the engine did not look".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

#: Packet categories, in the order they are searched.
CATEGORIES: tuple[str, ...] = (
    "company",
    "industry",
    "regulation",
    "policy",
    "forex",
    "macro",
)

DEFAULT_RESULTS_PER_QUERY = 6
DEFAULT_LOOKBACK_DAYS = 45
SUMMARY_CHARS = 600

#: Massive's news endpoint paginates the whole feed for a ticker (400+ rows on a
#: liquid name), most of it market round-ups that merely mention the symbol.
#: Keep the newest rows that are actually about this company.
MASSIVE_MAX_ITEMS = 15
MASSIVE_MAX_COTICKERS = 6


def _iso_days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%dT%H:%M:%SZ")


def _domain(url: str) -> str:
    try:
        host = urlparse(str(url)).netloc
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _trim(text: Any, *, limit: int = SUMMARY_CHARS) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3].rstrip()}..."


def build_queries(
    ticker: str,
    *,
    company_name: str | None = None,
    sector: str | None = None,
    industry: str | None = None,
) -> list[dict[str, str]]:
    """The six curated searches, specialised by whatever the profile knows."""
    name = (company_name or ticker).strip()
    field = (industry or sector or "the company's industry").strip().lower()
    return [
        {
            "category": "company",
            "query": (
                f"{name} ({ticker}) latest news: earnings, guidance, product launches, "
                "management changes, and analyst reaction"
            ),
        },
        {
            "category": "industry",
            "query": (
                f"{field} industry outlook: demand, pricing, capacity and competitive "
                f"position, including how it affects {name}"
            ),
        },
        {
            "category": "regulation",
            "query": (
                f"regulation, export controls, antitrust and litigation affecting the "
                f"{field} industry and {name}"
            ),
        },
        {
            "category": "policy",
            "query": (
                "Federal Reserve monetary policy decision, interest rate path, fiscal "
                "policy and government spending affecting equity markets"
            ),
        },
        {
            "category": "forex",
            "query": (
                f"US dollar, yen, euro and yuan currency moves and their effect on "
                f"{field} revenue and margins"
            ),
        },
        {
            "category": "macro",
            "query": (
                "upcoming scheduled economic decisions and data releases: FOMC meeting, "
                "CPI, nonfarm payrolls, and their expected market impact"
            ),
        },
    ]


def _exa_items(results: Sequence[Any], category: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for result in results:
        url = str(getattr(result, "url", "") or "").strip()
        if not url:
            continue
        items.append(
            {
                "title": _trim(getattr(result, "title", ""), limit=240) or url,
                "url": url,
                "published": getattr(result, "published_date", None),
                "source": _domain(url) or "exa",
                "summary": _trim(getattr(result, "snippet", "") or getattr(result, "text", "")),
                "category": category,
                "provider": "exa",
                "author": getattr(result, "author", None),
                "score": getattr(result, "score", None),
            }
        )
    return items


def _massive_items(
    payload: Mapping[str, Any] | None,
    *,
    ticker: str = "",
    max_items: int = MASSIVE_MAX_ITEMS,
    max_cotickers: int = MASSIVE_MAX_COTICKERS,
) -> list[dict[str, Any]]:
    """Newest Massive rows that are about this company, not just tagged with it.

    A story tagged with a dozen symbols is a market round-up; the memo wants the
    stories written about the name. Rows are already newest-first from the feed.
    """
    rows = (payload or {}).get("results")
    if not isinstance(rows, list):
        return []
    items: list[dict[str, Any]] = []
    symbol = str(ticker or "").strip().upper()
    for row in rows:
        if len(items) >= max(1, int(max_items)):
            break
        if not isinstance(row, Mapping):
            continue
        url = str(row.get("article_url") or "").strip()
        if not url:
            continue
        tickers = [str(item).upper() for item in (row.get("tickers") or []) if item]
        if symbol and tickers and (symbol not in tickers or len(tickers) > max_cotickers):
            continue
        publisher = row.get("publisher")
        source = ""
        if isinstance(publisher, Mapping):
            source = str(publisher.get("name") or "")
        items.append(
            {
                "title": _trim(row.get("title"), limit=240) or url,
                "url": url,
                "published": row.get("published_utc"),
                "source": source or _domain(url) or "massive",
                "summary": _trim(row.get("description")),
                "category": "company",
                "provider": "massive",
                "author": row.get("author"),
                "tickers": tickers[:8],
            }
        )
    return items


def _dedupe(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep the first sighting of each URL, preserving category order."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        url = str(item.get("url") or "")
        key = url.split("?", 1)[0].rstrip("/").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(dict(item))
    return out


def _published_key(item: Mapping[str, Any]) -> str:
    """Sort key that pushes undated items to the back without dropping them."""
    return str(item.get("published") or "0000")


def build_news(
    exa_client: Any | None,
    ticker: str,
    *,
    company_name: str | None = None,
    sector: str | None = None,
    industry: str | None = None,
    market_client: Any | None = None,
    results_per_query: int = DEFAULT_RESULTS_PER_QUERY,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_items: int = 40,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Build ``packet["news"]``.

    Never raises. A missing Exa key produces an empty item list, a populated
    ``query_log`` explaining why, and whatever Massive news is available.
    """
    symbol = str(ticker or "").strip().upper()
    fetched_at = datetime.now(UTC).isoformat()
    query_log: list[dict[str, Any]] = []
    collected: list[dict[str, Any]] = []
    errors: list[str] = []

    start = _iso_days_ago(lookback_days)
    if exa_client is None:
        query_log.append(
            {
                "category": "all",
                "query": None,
                "provider": "exa",
                "n_results": 0,
                "error": "Exa client is not configured",
            }
        )
        errors.append("Exa client is not configured")
    else:
        for spec in build_queries(
            symbol, company_name=company_name, sector=sector, industry=industry
        ):
            entry: dict[str, Any] = {
                "category": spec["category"],
                "query": spec["query"],
                "provider": "exa",
                "start_published_date": start,
                "n_results": 0,
                "error": None,
            }
            try:
                results = exa_client.search(
                    spec["query"],
                    num_results=int(results_per_query),
                    start_published_date=start,
                    category="news",
                )
            except Exception as exc:  # noqa: BLE001 - one dead bucket must not sink the rest
                entry["error"] = str(exc)
                errors.append(f"exa {spec['category']}: {exc}")
                query_log.append(entry)
                continue
            items = _exa_items(results, spec["category"])
            entry["n_results"] = len(items)
            query_log.append(entry)
            collected.extend(items)

    if market_client is not None:
        entry = {
            "category": "company",
            "query": f"massive /v2/reference/news?ticker={symbol}",
            "provider": "massive",
            "n_results": 0,
            "n_returned": 0,
            "filter": (
                f"newest {MASSIVE_MAX_ITEMS} rows tagged with {symbol} and at most "
                f"{MASSIVE_MAX_COTICKERS} tickers"
            ),
            "error": None,
        }
        try:
            payload = market_client.get_news(symbol, params={"limit": 20})
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)
            errors.append(f"massive news: {exc}")
            payload = None
        entry["n_returned"] = len((payload or {}).get("results") or [])
        items = _massive_items(payload, ticker=symbol)
        entry["n_results"] = len(items)
        query_log.append(entry)
        collected.extend(items)

    deduped = _dedupe(collected)
    deduped.sort(key=_published_key, reverse=True)
    items = deduped[: max(1, int(max_items))]

    counts: dict[str, int] = dict.fromkeys(CATEGORIES, 0)
    for item in items:
        category = str(item.get("category") or "company")
        counts[category] = counts.get(category, 0) + 1

    return {
        "fetched_at": fetched_at,
        "as_of": (as_of or datetime.now(UTC).date()).isoformat(),
        "lookback_days": int(lookback_days),
        "items": items,
        "counts_by_category": counts,
        "query_log": query_log,
        "providers": sorted({str(item.get("provider")) for item in items}),
        "errors": errors,
    }


def news_highlights(section: Mapping[str, Any] | None, *, limit: int = 8) -> list[dict[str, Any]]:
    """The most recent item from each category, for the memo projection."""
    items = list((section or {}).get("items") or [])
    if not items:
        return []
    picked: list[dict[str, Any]] = []
    seen_categories: set[str] = set()
    for item in items:
        category = str(item.get("category") or "company")
        if category in seen_categories:
            continue
        seen_categories.add(category)
        picked.append(item)
        if len(picked) >= limit:
            return picked
    for item in items:
        if item in picked:
            continue
        picked.append(item)
        if len(picked) >= limit:
            break
    return picked
