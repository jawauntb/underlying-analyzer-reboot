"""Benchmark universe resolution.

Prism compares one ticker against a fixed cross-asset frame: index, sector,
industry, commodity, FX, credit, rates, volatility and crypto. Fixing the frame
before comparing is the point — a beta or a correlation only means something
relative to a stated reference, so the same symbols are pulled for every ticker
and the ticker-specific part is only *which* sector/industry ETFs get promoted
to "related".

Two symbols in the user's original list do not resolve and are remapped here
with the reason carried into the packet:

``FXCH`` -> ``CYB``
    The WisdomTree Chinese Yuan fund (FXCH) was delisted; ``CYB`` is the
    surviving USD-listed onshore-renminbi ETF. FRED ``DEXCHUS`` carries the spot
    rate itself.
``VCHY`` -> ``HYG``
    ``VCHY`` does not resolve to a listed US security. US high-yield corporate
    credit is represented by ``HYG`` with FRED ``BAMLH0A0HYM2`` (ICE BofA
    high-yield option-adjusted spread) as the spread series.
"""

from __future__ import annotations

from typing import Any

from app.prism.contract import UniverseEntry

#: Symbols the user asked for that do not resolve, and what replaces them.
SYMBOL_REMAP: dict[str, dict[str, str]] = {
    "FXCH": {
        "symbol": "CYB",
        "note": (
            "FXCH (WisdomTree Chinese Yuan) is delisted; using CYB for the "
            "renminbi and FRED DEXCHUS for the spot rate."
        ),
    },
    "VCHY": {
        "symbol": "HYG",
        "note": (
            "VCHY does not resolve to a listed US security; using HYG for US "
            "high-yield corporate credit and FRED BAMLH0A0HYM2 for the spread."
        ),
    },
}

#: Massive returns 403 for index tickers, so the volatility level comes from FRED.
INDEX_SYMBOL_NOTES: dict[str, str] = {
    "VIX": "Massive returns 403 for I:VIX; VIX level is read from FRED VIXCLS.",
}

#: Coverage limits verified against the live Massive plan on 2026-09-01. These are
#: entitlement facts, not guesses: the loader still asks for the full window and
#: reports whatever actually comes back, but the packet carries the reason.
COVERAGE_NOTES: dict[str, str] = {
    "X:BTCUSD": (
        "Massive returns roughly 2 years of daily crypto bars on this plan "
        "(status DELAYED); long-window statistics for bitcoin are unavailable."
    ),
    "CYB": (
        "CYB stopped trading in October 2023; its history is still usable for "
        "long-window correlation, but the live renminbi level is FRED DEXCHUS."
    ),
    "FSCHX": (
        "Massive returns no daily bars for mutual funds on this plan; the "
        "chemicals sleeve falls back to XLB."
    ),
    "VMIAX": (
        "Massive returns no daily bars for mutual funds on this plan; the "
        "materials sleeve falls back to XLB."
    ),
}


class Benchmark:
    """One benchmark in the fixed comparison frame."""

    __slots__ = ("symbol", "label", "role", "provider", "note")

    def __init__(
        self,
        symbol: str,
        label: str,
        role: str,
        *,
        provider: str = "massive",
        note: str | None = None,
    ) -> None:
        self.symbol = symbol
        self.label = label
        self.role = role
        self.provider = provider
        self.note = note

    def entry(self) -> UniverseEntry:
        """Packet-shaped entry with no coverage numbers filled in yet."""
        return UniverseEntry(
            symbol=self.symbol,
            label=self.label,
            role=self.role,
            provider=self.provider,
            first_date=None,
            last_date=None,
            n_days=0,
            note=self.note,
            error=None,
        )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Benchmark({self.symbol!r}, {self.role!r})"


BENCHMARKS: tuple[Benchmark, ...] = (
    # Indices
    Benchmark("SPY", "S&P 500", "index"),
    Benchmark("DIA", "Dow Jones Industrial Average", "index"),
    Benchmark("QQQ", "Nasdaq 100", "index"),
    Benchmark("IWM", "Russell 2000", "index"),
    # Sectors (SPDR Select Sector)
    Benchmark("XLK", "Technology", "sector"),
    Benchmark("XLY", "Consumer Discretionary", "sector"),
    Benchmark("XLV", "Health Care", "sector"),
    Benchmark("XLU", "Utilities", "sector"),
    Benchmark("XLB", "Materials", "sector"),
    Benchmark("XLC", "Communication Services", "sector"),
    Benchmark("XLF", "Financials", "sector"),
    Benchmark("XLP", "Consumer Staples", "sector"),
    Benchmark("XLE", "Energy", "sector"),
    Benchmark("XLI", "Industrials", "sector"),
    Benchmark("XLRE", "Real Estate", "sector"),
    # Industry / thematic
    Benchmark("SOXX", "Semiconductors", "industry"),
    Benchmark("XBI", "Biotechnology", "industry"),
    Benchmark("FSCHX", "Fidelity Select Chemicals", "industry", note=COVERAGE_NOTES["FSCHX"]),
    Benchmark(
        "VMIAX",
        "Vanguard Materials Index Admiral",
        "industry",
        note=COVERAGE_NOTES["VMIAX"],
    ),
    Benchmark("REMX", "Rare Earth & Strategic Metals", "industry"),
    Benchmark("URA", "Uranium", "industry"),
    # Commodities
    Benchmark("DBB", "Base Metals", "commodity"),
    Benchmark("CPER", "Copper", "commodity"),
    Benchmark("DBC", "Broad Commodities", "commodity"),
    Benchmark("DBA", "Agriculture", "commodity"),
    Benchmark("TAGS", "Agriculture (grains basket)", "commodity"),
    Benchmark("GLD", "Gold", "gold"),
    Benchmark("USO", "WTI Crude Oil", "commodity"),
    Benchmark("BNO", "Brent Crude Oil", "commodity"),
    # FX
    Benchmark("UUP", "US Dollar Index (fund)", "fx"),
    Benchmark("FXY", "Japanese Yen", "fx"),
    Benchmark("DXJ", "Japan Equities (yen hedged)", "fx"),
    Benchmark("VGK", "Europe Equities", "fx"),
    Benchmark("FXE", "Euro", "fx"),
    Benchmark(
        "CYB",
        "Chinese Renminbi",
        "fx",
        note=f"{SYMBOL_REMAP['FXCH']['note']} {COVERAGE_NOTES['CYB']}",
    ),
    Benchmark("FXF", "Swiss Franc", "fx"),
    Benchmark("FXC", "Canadian Dollar", "fx"),
    Benchmark("FXA", "Australian Dollar", "fx"),
    # Credit
    Benchmark("HYG", "US High Yield Corporate", "credit", note=SYMBOL_REMAP["VCHY"]["note"]),
    Benchmark("LQD", "US Investment Grade Corporate", "credit"),
    Benchmark("TLT", "20+ Year Treasuries", "credit"),
    # Crypto
    Benchmark("X:BTCUSD", "Bitcoin (USD)", "crypto", note=COVERAGE_NOTES["X:BTCUSD"]),
)

BENCHMARKS_BY_SYMBOL: dict[str, Benchmark] = {item.symbol: item for item in BENCHMARKS}

#: FRED-sourced members of the universe (no Massive price series).
FRED_BENCHMARKS: tuple[Benchmark, ...] = (
    Benchmark("DGS2", "2-Year Treasury Yield", "rates", provider="fred"),
    Benchmark("DGS5", "5-Year Treasury Yield", "rates", provider="fred"),
    Benchmark("DGS10", "10-Year Treasury Yield", "rates", provider="fred"),
    Benchmark("DGS20", "20-Year Treasury Yield", "rates", provider="fred"),
    Benchmark("T10Y2Y", "10Y minus 2Y Spread", "rates", provider="fred"),
    Benchmark(
        "VIXCLS",
        "CBOE Volatility Index",
        "vol",
        provider="fred",
        note=INDEX_SYMBOL_NOTES["VIX"],
    ),
    Benchmark("DTWEXBGS", "Broad Trade-Weighted Dollar", "macro", provider="fred"),
    Benchmark("DCOILWTICO", "WTI Crude Spot", "macro", provider="fred"),
    Benchmark("DCOILBRENTEU", "Brent Crude Spot", "macro", provider="fred"),
    Benchmark("BAMLH0A0HYM2", "US High Yield OAS", "macro", provider="fred"),
    Benchmark("PAYEMS", "Nonfarm Payrolls", "macro", provider="fred"),
)

FRED_BENCHMARKS_BY_ID: dict[str, Benchmark] = {item.symbol: item for item in FRED_BENCHMARKS}

#: The primary reference frame. Every gauge-fixed comparison is excess over this.
REFERENCE_SYMBOL = "SPY"

#: Sector name (as reported by Massive/SEC/Yahoo profiles) -> SPDR sector ETF.
SECTOR_ETFS: dict[str, str] = {
    "technology": "XLK",
    "information technology": "XLK",
    "tech": "XLK",
    "consumer discretionary": "XLY",
    "consumer cyclical": "XLY",
    "healthcare": "XLV",
    "health care": "XLV",
    "utilities": "XLU",
    "basic materials": "XLB",
    "materials": "XLB",
    "communication services": "XLC",
    "communications": "XLC",
    "financial services": "XLF",
    "financials": "XLF",
    "financial": "XLF",
    "consumer staples": "XLP",
    "consumer defensive": "XLP",
    "energy": "XLE",
    "industrials": "XLI",
    "industrial": "XLI",
    "real estate": "XLRE",
}

#: Keyword found in an industry / SIC description -> thematic ETF.
INDUSTRY_ETFS: tuple[tuple[str, str], ...] = (
    ("semiconductor", "SOXX"),
    ("semi-conductor", "SOXX"),
    ("microprocessor", "SOXX"),
    ("integrated circuit", "SOXX"),
    ("electronic computer", "SOXX"),
    ("biotech", "XBI"),
    ("biological product", "XBI"),
    ("pharmaceutical", "XBI"),
    ("uranium", "URA"),
    ("nuclear", "URA"),
    ("rare earth", "REMX"),
    ("metal mining", "REMX"),
    ("mining", "REMX"),
    # FSCHX has no Massive daily bars on this plan, so chemicals map to XLB.
    ("chemical", "XLB"),
    ("industrial gases", "XLB"),
    ("copper", "CPER"),
    ("gold", "GLD"),
    ("petroleum", "USO"),
    ("crude", "USO"),
    ("oil and gas", "USO"),
    ("agricultur", "DBA"),
    ("farm", "DBA"),
    ("real estate investment trust", "XLRE"),
)

#: Always-on comparison set, in the order the dashboard shows them.
CORE_BENCHMARKS: tuple[str, ...] = ("SPY", "QQQ", "IWM", "DIA")


def normalize_symbol(symbol: str) -> str:
    """Uppercase and remap a user-supplied symbol onto a resolvable one."""
    cleaned = str(symbol or "").strip().upper()
    if not cleaned:
        raise ValueError("symbol is required")
    remap = SYMBOL_REMAP.get(cleaned)
    return remap["symbol"] if remap else cleaned


def remap_note(symbol: str) -> str | None:
    """Return the human-readable reason a symbol was remapped, if it was."""
    remap = SYMBOL_REMAP.get(str(symbol or "").strip().upper())
    return remap["note"] if remap else None


def sector_etf(sector: str | None) -> str | None:
    """Map a profile sector string onto its SPDR sector ETF."""
    if not sector:
        return None
    key = str(sector).strip().lower()
    if key in SECTOR_ETFS:
        return SECTOR_ETFS[key]
    for name, etf in SECTOR_ETFS.items():
        if name in key:
            return etf
    return None


def industry_etfs(*descriptions: str | None) -> list[str]:
    """Map free-text industry / SIC descriptions onto thematic ETFs."""
    haystack = " ".join(str(part).lower() for part in descriptions if part)
    if not haystack:
        return []
    found: list[str] = []
    for keyword, etf in INDUSTRY_ETFS:
        if keyword in haystack and etf not in found:
            found.append(etf)
    return found


def related_etfs(profile: dict[str, Any] | None, *, limit: int = 6) -> list[str]:
    """Pick the ETFs a ticker should be read against, most specific first.

    The order is: thematic industry ETFs (SOXX for semis, XBI for biotech, …),
    then the sector ETF, then the broad index sleeve. ``limit`` bounds the list
    so the dashboard's seasonality grid stays readable.
    """
    profile = profile or {}
    sector = profile.get("sector") or profile.get("Sector")
    industry = profile.get("industry") or profile.get("Industry")
    description = profile.get("description") or profile.get("longBusinessSummary")
    ordered: list[str] = []
    for etf in industry_etfs(industry, description):
        if etf not in ordered:
            ordered.append(etf)
    sector_symbol = sector_etf(sector) or sector_etf(industry)
    if sector_symbol and sector_symbol not in ordered:
        ordered.append(sector_symbol)
    for symbol in ("QQQ", "SPY"):
        if symbol not in ordered:
            ordered.append(symbol)
    return ordered[:limit]


def benchmark_symbols(
    *,
    roles: tuple[str, ...] | None = None,
    include_crypto: bool = True,
) -> list[str]:
    """Massive-sourced benchmark symbols, optionally filtered by role."""
    symbols: list[str] = []
    for item in BENCHMARKS:
        if roles is not None and item.role not in roles:
            continue
        if not include_crypto and item.role == "crypto":
            continue
        symbols.append(item.symbol)
    return symbols


def fred_series_ids(*, roles: tuple[str, ...] | None = None) -> list[str]:
    """FRED series that belong to the universe, optionally filtered by role."""
    return [
        item.symbol for item in FRED_BENCHMARKS if roles is None or item.role in roles
    ]


def resolve_universe(
    ticker: str,
    *,
    profile: dict[str, Any] | None = None,
    include_fred: bool = True,
) -> list[UniverseEntry]:
    """Return the packet's ``universe`` list for one ticker.

    The ticker itself leads the list (role ``index`` is reserved for the market
    proxies, so the subject carries role ``self``), followed by every Massive
    benchmark and — when ``include_fred`` — the FRED rates/vol/macro members.
    Coverage fields (``first_date``/``last_date``/``n_days``) stay zeroed until
    :func:`app.prism.data.load_universe` fills them from real responses.
    """
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        raise ValueError("ticker is required")
    entries: list[UniverseEntry] = [
        UniverseEntry(
            symbol=symbol,
            label=str((profile or {}).get("name") or symbol),
            role="self",
            provider="massive",
            first_date=None,
            last_date=None,
            n_days=0,
            note=None,
            error=None,
        )
    ]
    related = set(related_etfs(profile))
    for item in BENCHMARKS:
        if item.symbol == symbol:
            continue
        entry = item.entry()
        if item.symbol in related:
            note = entry.get("note")
            related_note = f"related ETF for {symbol}"
            entry["note"] = f"{note}; {related_note}" if note else related_note
        entries.append(entry)
    if include_fred:
        entries.extend(item.entry() for item in FRED_BENCHMARKS)
    return entries


def universe_symbols(
    ticker: str,
    *,
    profile: dict[str, Any] | None = None,
    include_crypto: bool = True,
) -> list[str]:
    """Every Massive symbol to download for one ticker (subject first)."""
    symbol = str(ticker or "").strip().upper()
    symbols = [symbol]
    for candidate in benchmark_symbols(include_crypto=include_crypto):
        if candidate != symbol and candidate not in symbols:
            symbols.append(candidate)
    for candidate in related_etfs(profile):
        if candidate != symbol and candidate not in symbols:
            symbols.append(candidate)
    return symbols
