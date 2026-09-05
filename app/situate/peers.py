"""A static, curated cross-sectional peer universe (breadth for the stack).

The stack (:mod:`app.situate.stack`) needs *breadth* — many names observed at
each month-end — to have any hope of a non-spurious cross-sectional edge
(Grinold's ``IR ≈ IC·√breadth``). Sector-ETF constituents are not available from
Massive on this plan, so this module hard-codes a small, stable, hand-curated
universe: the current S&P 100 grouped by GICS sector, which doubles as the
per-sector peer lists. It is deliberately static and versioned rather than
scraped, so the walk-forward panel is reproducible and does not depend on a live
constituents feed.

Each sector maps to its SPDR sector ETF (the "industry ETF" the stack takes
excess returns over). The mapping reuses Prism's :data:`app.prism.universe.SECTOR_ETFS`
when importable, with a local copy as a fallback so this module has no hard
dependency on Prism import order.

Nothing here fetches data or fabricates a value; it is a pure lookup table.
"""

from __future__ import annotations

__all__ = [
    "PEERS_VERSION",
    "PEERS_BY_SECTOR",
    "SECTOR_ETF",
    "SECTOR_BY_TICKER",
    "SP100",
    "sector_of",
    "industry_etf_of",
    "peers_for",
    "default_universe",
    "universe_for",
    "etf_map",
    "all_sector_etfs",
]

PEERS_VERSION = "1.0.0"

#: Sector name -> SPDR sector ETF. A local copy of the subset of
#: :data:`app.prism.universe.SECTOR_ETFS` that this universe uses; the live map is
#: preferred at call time and this is the fallback.
SECTOR_ETF: dict[str, str] = {
    "technology": "XLK",
    "communication services": "XLC",
    "consumer discretionary": "XLY",
    "consumer staples": "XLP",
    "financials": "XLF",
    "healthcare": "XLV",
    "industrials": "XLI",
    "energy": "XLE",
    "materials": "XLB",
    "utilities": "XLU",
    "real estate": "XLRE",
}

#: Current S&P 100 (as of the 2026 build), grouped by GICS sector. These lists ARE
#: the per-sector peer lists. Kept static and versioned for reproducibility.
PEERS_BY_SECTOR: dict[str, tuple[str, ...]] = {
    "technology": (
        "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "ADBE", "CSCO",
        "ACN", "AMD", "INTC", "IBM", "TXN", "QCOM", "INTU", "NOW",
    ),
    "communication services": (
        "GOOGL", "GOOG", "META", "NFLX", "DIS", "CMCSA", "T", "VZ", "TMUS", "CHTR",
    ),
    "consumer discretionary": (
        "AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX", "BKNG", "TGT", "GM", "F",
    ),
    "consumer staples": (
        "PG", "KO", "PEP", "COST", "WMT", "PM", "MO", "MDLZ", "CL", "KMB", "GIS",
    ),
    "financials": (
        "BRK.B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "AXP", "C",
        "SCHW", "BLK", "SPGI", "CB", "PYPL", "COF", "USB",
    ),
    "healthcare": (
        "UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR",
        "BMY", "AMGN", "GILD", "CVS", "MDT",
    ),
    "industrials": (
        "BA", "CAT", "GE", "HON", "UPS", "RTX", "UNP", "LMT", "DE", "MMM",
        "GD", "EMR", "FDX",
    ),
    "energy": ("XOM", "CVX", "COP", "SLB"),
    "materials": ("LIN", "APD", "SHW", "FCX", "NEM", "DOW"),
    "utilities": ("NEE", "DUK", "SO", "AEP", "EXC"),
    "real estate": ("AMT", "PLD", "SPG"),
}


def _build_sector_by_ticker() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for sector, tickers in PEERS_BY_SECTOR.items():
        for ticker in tickers:
            mapping[ticker] = sector
    return mapping


#: Ticker -> sector name (derived from :data:`PEERS_BY_SECTOR`).
SECTOR_BY_TICKER: dict[str, str] = _build_sector_by_ticker()

#: The flattened S&P 100 universe, de-duplicated in sector order.
SP100: tuple[str, ...] = tuple(SECTOR_BY_TICKER.keys())


def _normalize(ticker: str | None) -> str:
    return str(ticker or "").strip().upper()


def sector_of(ticker: str | None) -> str | None:
    """The curated sector for ``ticker`` (``None`` when not in the universe)."""
    return SECTOR_BY_TICKER.get(_normalize(ticker))


def _sector_etf_lookup(sector: str) -> str | None:
    """Resolve a sector name to its ETF, preferring Prism's live map."""
    try:
        from app.prism.universe import sector_etf as _prism_sector_etf

        etf = _prism_sector_etf(sector)
        if etf:
            return str(etf)
    except Exception:  # noqa: BLE001 - fall back to the local copy
        pass
    return SECTOR_ETF.get(sector)


def industry_etf_of(ticker: str | None) -> str | None:
    """The sector ("industry") ETF the stack takes excess returns over.

    ``None`` when the ticker is not in the curated universe or its sector has no
    mapped ETF — the caller must then drop the name from the cross-section rather
    than invent a benchmark.
    """
    sector = sector_of(ticker)
    if sector is None:
        return None
    return _sector_etf_lookup(sector)


def peers_for(ticker: str | None, *, include_self: bool = True) -> list[str]:
    """The sector peer list for ``ticker`` (empty when the sector is unknown)."""
    sector = sector_of(ticker)
    if sector is None:
        return []
    peers = list(PEERS_BY_SECTOR[sector])
    if not include_self:
        symbol = _normalize(ticker)
        peers = [p for p in peers if p != symbol]
    return peers


def default_universe(limit: int | None = None) -> list[str]:
    """The full S&P 100 universe, optionally truncated to ``limit`` names."""
    names = list(SP100)
    if limit is not None and limit > 0:
        names = names[: int(limit)]
    return names


def universe_for(
    ticker: str | None = None,
    *,
    sector: str | None = None,
    include_sp100: bool = False,
    limit: int | None = None,
) -> list[str]:
    """Assemble the cross-sectional universe to fit the stack over.

    Priority: an explicit ``sector`` list, else ``ticker``'s sector peers (with
    ``ticker`` guaranteed present even if it is not itself in the S&P 100), else
    the full S&P 100. ``include_sp100`` unions the broad universe onto the sector
    peers for extra breadth; ``limit`` caps the result (the caller logs the
    truncation).
    """
    names: list[str] = []
    if sector is not None:
        key = str(sector).strip().lower()
        names = list(PEERS_BY_SECTOR.get(key, ()))
    elif ticker is not None:
        symbol = _normalize(ticker)
        names = peers_for(symbol)
        if symbol and symbol not in names:
            names = [symbol, *names]
        if include_sp100:
            for name in SP100:
                if name not in names:
                    names.append(name)
    else:
        names = list(SP100)

    if not names:
        names = list(SP100)

    # De-duplicate preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    if limit is not None and limit > 0:
        ordered = ordered[: int(limit)]
    return ordered


def etf_map(symbols: list[str] | tuple[str, ...] | None = None) -> dict[str, str | None]:
    """Map each symbol to its sector ETF (``None`` when unknown)."""
    universe = list(symbols) if symbols is not None else list(SP100)
    return {sym: industry_etf_of(sym) for sym in universe}


def all_sector_etfs() -> list[str]:
    """The distinct sector ETFs referenced by the universe, in a stable order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for sector in PEERS_BY_SECTOR:
        etf = _sector_etf_lookup(sector)
        if etf and etf not in seen:
            seen.add(etf)
            ordered.append(etf)
    return ordered
