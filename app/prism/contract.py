"""The ``PrismPacket`` contract.

Every workstream codes against the names in this module rather than against each
other's implementations, so a section that is not built yet is still present in
the packet as ``None`` with a sibling ``<section>_error`` string. That keeps the
packet shape stable for the API proxy, the iOS dashboard and the agent tools even
when a data source is down.

Conventions
-----------
* Percent returns are decimal fractions (``0.034`` is ``3.4%``).
* Dates are ISO-8601 strings; timestamps are ISO-8601 UTC with ``+00:00``.
* Absent numbers are ``None``, never ``0`` and never a placeholder — the engine
  must be able to say "we do not know" without lying about a price.
* Every fetched number carries provenance in ``sources`` and in the per-section
  ``provider``/``series_id``/``fetched_at`` fields.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal, TypedDict

ENGINE_NAME = "Prism"
ENGINE_ALIAS = "ubermemo"
ENGINE_VERSION = "1.0.0"

#: Top-level keys of the packet, in the order they are documented in the plan.
#: ``empty_packet()`` always returns exactly these keys.
PACKET_KEYS: tuple[str, ...] = (
    "ticker",
    "as_of",
    "generated_at",
    "engine_version",
    "name",
    "profile",
    "universe",
    "seasonality",
    "macro",
    "relational",
    "factors",
    "regimes",
    "entropy",
    "spectral",
    "eigen",
    "fundamentals",
    "filings",
    "volatility",
    "levels",
    "news",
    "scenarios",
    "recent",
    "memo",
    "sources",
    "meta",
)

#: Sections that may be ``None`` and therefore carry a sibling ``*_error`` key.
NULLABLE_SECTIONS: tuple[str, ...] = (
    "profile",
    "seasonality",
    "macro",
    "relational",
    "factors",
    "regimes",
    "entropy",
    "spectral",
    "eigen",
    "fundamentals",
    "filings",
    "volatility",
    "levels",
    "news",
    "scenarios",
    "recent",
    "memo",
)

#: Named trailing windows shared by seasonality, relational and factor code.
WINDOW_DAYS: dict[str, int] = {
    "1m": 21,
    "2m": 42,
    "3m": 63,
    "6m": 126,
    "1y": 252,
    "2y": 504,
    "3y": 756,
    "5y": 1260,
    "10y": 2520,
}

#: Forward horizons used by seasonality, spectral projection and scenarios.
HORIZONS: tuple[str, ...] = ("1m", "2m", "3m", "6m", "12m", "18m")

#: Months in each forward horizon label.
HORIZON_MONTHS: dict[str, int] = {"1m": 1, "2m": 2, "3m": 3, "6m": 6, "12m": 12, "18m": 18}

#: Seasonality look-back windows, in years.
SEASONAL_WINDOWS: tuple[int, ...] = (1, 2, 5, 10)

UniverseRole = Literal[
    "self",
    "index",
    "sector",
    "industry",
    "commodity",
    "fx",
    "rates",
    "vol",
    "crypto",
    "credit",
    "gold",
    "macro",
]

RecommendationAction = Literal["strong_buy", "buy", "hold", "sell", "strong_sell"]


class Provenance(TypedDict, total=False):
    """One row of the packet's ``sources`` list."""

    provider: str
    url: str | None
    series_id: str | None
    symbol: str | None
    fetched_at: str
    confidence: float | None
    note: str | None


class UniverseEntry(TypedDict, total=False):
    """One resolved benchmark in ``packet["universe"]``."""

    symbol: str
    label: str
    role: str
    provider: str
    first_date: str | None
    last_date: str | None
    n_days: int
    note: str | None
    error: str | None


class MonthlyPoint(TypedDict):
    """One month inside ``MacroSeries["monthly_12"]``."""

    month: str
    value: float | None
    avg: float | None
    change: float | None


class MacroSeries(TypedDict, total=False):
    """A macro or benchmark level series compressed to what a memo can cite."""

    series_id: str
    label: str
    provider: str
    units: str | None
    change_mode: str
    current: float | None
    as_of: str | None
    change_1m: float | None
    change_3m: float | None
    change_12m: float | None
    monthly_12: list[MonthlyPoint]
    n_observations: int
    error: str | None


class SeasonalWindowStats(TypedDict):
    """This-calendar-month statistics over one look-back window."""

    mean: float | None
    median: float | None
    n: int
    hit_rate: float | None
    values: list[dict[str, float]]


class SeasonalTrend(TypedDict):
    """Whether the calendar-month edge is strengthening or fading."""

    direction: str
    slope: float | None
    windows_used: list[int]


class SeasonalForwardStats(TypedDict):
    """Forward-return distribution conditional on starting in this month."""

    mean: float | None
    median: float | None
    n: int
    hit_rate: float | None
    p10: float | None
    p90: float | None


class SeasonalStats(TypedDict, total=False):
    """``packet["seasonality"]["ticker"]`` and each benchmark entry."""

    symbol: str
    month: int
    month_label: str
    n_years: int
    first_date: str | None
    last_date: str | None
    this_month: dict[str, SeasonalWindowStats]
    trend: SeasonalTrend
    forward: dict[str, SeasonalForwardStats]
    error: str | None


class PacketMeta(TypedDict, total=False):
    """``packet["meta"]`` — honest bookkeeping about what actually ran."""

    errors: list[dict[str, str]]
    source_status: dict[str, str]
    timings_ms: dict[str, float]
    cache: dict[str, str]
    unavailable: list[dict[str, str]]
    notes: list[str]


MONTH_LABELS: tuple[str, ...] = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def month_label(month: int) -> str:
    """Return the English label for a 1-based calendar month."""
    if not 1 <= int(month) <= 12:
        raise ValueError("month must be between 1 and 12")
    return MONTH_LABELS[int(month) - 1]


def utc_now_iso() -> str:
    """Current UTC timestamp as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def empty_macro_series(
    series_id: str,
    *,
    provider: str = "fred",
    label: str | None = None,
    units: str | None = None,
    change_mode: str = "diff",
    error: str | None = None,
) -> MacroSeries:
    """A ``MacroSeries`` with every key present and no fabricated values."""
    return MacroSeries(
        series_id=series_id,
        label=label or series_id,
        provider=provider,
        units=units,
        change_mode=change_mode,
        current=None,
        as_of=None,
        change_1m=None,
        change_3m=None,
        change_12m=None,
        monthly_12=[],
        n_observations=0,
        error=error,
    )


def empty_seasonal_window() -> SeasonalWindowStats:
    """A this-month window block with no observations."""
    return SeasonalWindowStats(mean=None, median=None, n=0, hit_rate=None, values=[])


def empty_seasonal_forward() -> SeasonalForwardStats:
    """A forward-horizon block with no observations."""
    return SeasonalForwardStats(mean=None, median=None, n=0, hit_rate=None, p10=None, p90=None)


def empty_seasonal_stats(
    symbol: str,
    *,
    month: int,
    error: str | None = None,
) -> SeasonalStats:
    """A ``SeasonalStats`` skeleton with all windows and horizons present."""
    return SeasonalStats(
        symbol=symbol,
        month=int(month),
        month_label=month_label(month),
        n_years=0,
        first_date=None,
        last_date=None,
        this_month={f"{years}y": empty_seasonal_window() for years in SEASONAL_WINDOWS},
        trend=SeasonalTrend(direction="flat", slope=None, windows_used=[]),
        forward={horizon: empty_seasonal_forward() for horizon in HORIZONS},
        error=error,
    )


def empty_meta() -> PacketMeta:
    """A fresh ``meta`` block."""
    return PacketMeta(
        errors=[],
        source_status={},
        timings_ms={},
        cache={},
        unavailable=[],
        notes=[],
    )


def empty_packet(ticker: str, *, as_of: date | str | None = None) -> dict[str, Any]:
    """Return a packet with every contract key present and no invented numbers.

    Sections start as ``None``; ``set_section`` fills them in and records a
    ``<section>_error`` when a builder fails, so consumers can always index the
    same keys.
    """
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        raise ValueError("ticker is required")
    resolved_as_of = as_of.isoformat() if isinstance(as_of, date) else (as_of or None)
    packet: dict[str, Any] = {
        "ticker": symbol,
        "as_of": resolved_as_of,
        "generated_at": utc_now_iso(),
        "engine_version": ENGINE_VERSION,
        "name": ENGINE_NAME,
        "universe": [],
        "sources": [],
        "meta": empty_meta(),
    }
    for section in NULLABLE_SECTIONS:
        packet[section] = None
        packet[f"{section}_error"] = None
    return {key: packet[key] for key in PACKET_KEYS} | {
        f"{section}_error": packet[f"{section}_error"] for section in NULLABLE_SECTIONS
    }


def set_section(
    packet: dict[str, Any],
    section: str,
    value: Any,
    *,
    error: str | None = None,
) -> dict[str, Any]:
    """Write one section (and its ``*_error`` sibling) into ``packet``.

    Passing ``error`` leaves the section ``None`` and records the reason both
    beside the section and in ``meta.errors``, which is how the engine keeps a
    single failing source from failing the whole build.
    """
    packet[section] = None if error else value
    if section in NULLABLE_SECTIONS:
        packet[f"{section}_error"] = error
    if error:
        record_error(packet, section, error)
    return packet


def record_error(packet: dict[str, Any], source: str, error: str) -> dict[str, Any]:
    """Append one honest failure to ``meta.errors`` and mark the source status."""
    meta = packet.setdefault("meta", empty_meta())
    errors = meta.setdefault("errors", [])
    entry = {"source": str(source), "error": str(error)[:500]}
    if entry not in errors:
        errors.append(entry)
    meta.setdefault("source_status", {})[str(source)] = "error"
    return packet


def record_source(packet: dict[str, Any], source: Provenance) -> dict[str, Any]:
    """Append a provenance row to ``packet["sources"]`` (deduplicated)."""
    sources = packet.setdefault("sources", [])
    if source not in sources:
        sources.append(dict(source))
    return packet


def record_timing(packet: dict[str, Any], name: str, milliseconds: float) -> dict[str, Any]:
    """Record how long one section took, for the ``meta.timings_ms`` block."""
    meta = packet.setdefault("meta", empty_meta())
    meta.setdefault("timings_ms", {})[str(name)] = round(float(milliseconds), 3)
    return packet


def record_unavailable(packet: dict[str, Any], source: str, reason: str) -> dict[str, Any]:
    """Record a source that is genuinely unavailable rather than broken."""
    meta = packet.setdefault("meta", empty_meta())
    unavailable = meta.setdefault("unavailable", [])
    entry = {"source": str(source), "reason": str(reason)[:500]}
    if entry not in unavailable:
        unavailable.append(entry)
    meta.setdefault("source_status", {})[str(source)] = "unavailable"
    return packet


def validate_packet(packet: dict[str, Any]) -> list[str]:
    """Return a list of contract violations (empty when the packet is valid)."""
    problems: list[str] = []
    for key in PACKET_KEYS:
        if key not in packet:
            problems.append(f"missing key: {key}")
    for section in NULLABLE_SECTIONS:
        error_key = f"{section}_error"
        if error_key not in packet:
            problems.append(f"missing key: {error_key}")
            continue
        if packet.get(section) is None and packet.get(error_key) is None:
            continue
        if packet.get(section) is not None and packet.get(error_key) is not None:
            problems.append(f"{section} is populated but {error_key} is also set")
    meta = packet.get("meta")
    if not isinstance(meta, dict):
        problems.append("meta must be an object")
    elif not isinstance(meta.get("errors"), list):
        problems.append("meta.errors must be a list")
    if not isinstance(packet.get("sources"), list):
        problems.append("sources must be a list")
    if not isinstance(packet.get("universe"), list):
        problems.append("universe must be a list")
    return problems
