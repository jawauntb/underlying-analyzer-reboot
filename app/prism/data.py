"""Daily price loading, alignment and provenance for Prism.

Everything downstream of this module reads a plain ``pd.Series`` of adjusted
closes indexed by naive UTC dates. The rules that make that possible:

* **Massive only.** Yahoo/yfinance is unreachable from the engine's network and
  must never be a dependency, so the Prism client is built with the facade's
  legacy fallback disabled (:func:`build_prism_client`). A short Massive history
  is reported honestly as short coverage instead of being silently patched from
  another provider.
* **Colon symbols.** ``MarketDataClient.get_history`` runs its argument through
  ``clean_ticker``, whose symbol pattern rejects ``:``. Crypto (``X:BTCUSD``) and
  FX (``C:EURUSD``) pairs are therefore routed straight to the underlying
  provider, which passes the symbol through to
  ``/v2/aggs/ticker/{symbol}/range/1/day/...`` unchanged.
* **Shrink, do not fake.** A symbol listed five years ago cannot return ten
  years. The loader retries with progressively shorter windows and records what
  it actually got (``first_date``/``last_date``/``n_days``) rather than padding.
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

import numpy as np
import pandas as pd

from app.prism.cache import PrismCache, as_of_month
from app.prism.contract import WINDOW_DAYS, Provenance, UniverseEntry

TRADING_DAYS_PER_YEAR = 252
CALENDAR_DAYS_PER_YEAR = 365.25
DEFAULT_YEARS = 10
DEFAULT_MAX_WORKERS = 6
MIN_USABLE_POINTS = 20
#: A series whose last observation is older than this (in calendar days) is
#: flagged stale — a delisted fund, not a live quote.
STALE_GAP_DAYS = 30
#: Window lengths (in years) tried in order when the full request comes back empty.
FALLBACK_YEARS: tuple[int, ...] = (10, 5, 2, 1)


class PrismDataError(RuntimeError):
    """No usable daily history could be loaded for a symbol."""


class HistorySource(Protocol):
    """The only market-data surface Prism's loader needs."""

    def get_history(self, ticker: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class SeriesLoad:
    """One symbol's adjusted closes plus everything needed to cite them."""

    symbol: str
    series: pd.Series
    provider: str
    note: str
    first_date: str | None
    last_date: str | None
    n_days: int
    requested_years: int
    as_of: str | None = None
    cached: bool = False
    error: str | None = None

    @property
    def stale_days(self) -> int | None:
        """Calendar days between the last observation and the as-of date."""
        if not self.last_date or not self.as_of:
            return None
        return (date.fromisoformat(self.as_of) - date.fromisoformat(self.last_date)).days

    def is_stale(self, *, max_gap_days: int = STALE_GAP_DAYS) -> bool:
        """True when the series stopped updating well before the as-of date."""
        gap = self.stale_days
        return gap is not None and gap > max_gap_days

    @property
    def coverage_years(self) -> float:
        """Calendar years actually covered by the returned series."""
        if not self.first_date or not self.last_date:
            return 0.0
        span = date.fromisoformat(self.last_date) - date.fromisoformat(self.first_date)
        return round(span.days / CALENDAR_DAYS_PER_YEAR, 3)

    def provenance(self) -> Provenance:
        """A row for ``packet["sources"]``."""
        return Provenance(
            provider=self.provider,
            url=None,
            series_id=None,
            symbol=self.symbol,
            fetched_at=datetime.now(UTC).isoformat(),
            confidence=None if self.error else 1.0,
            note=self.note,
        )

    def universe_entry(
        self, *, label: str, role: str, max_gap_days: int = STALE_GAP_DAYS
    ) -> UniverseEntry:
        """A row for ``packet["universe"]``.

        A series that stopped updating (a delisted ETF, say) keeps its history —
        it is still usable for long-window correlation — but the note says so, so
        nothing downstream quotes a two-year-old close as "current".
        """
        note = self.note
        if self.is_stale(max_gap_days=max_gap_days):
            stale_note = (
                f"stale: last observation {self.last_date} is {self.stale_days} days "
                f"before {self.as_of}"
            )
            note = f"{note}; {stale_note}" if note else stale_note
        return UniverseEntry(
            symbol=self.symbol,
            label=label,
            role=role,
            provider=self.provider,
            first_date=self.first_date,
            last_date=self.last_date,
            n_days=self.n_days,
            note=note,
            error=self.error,
        )


@dataclass
class UniverseData:
    """The aligned result of loading many symbols at once."""

    series: dict[str, pd.Series] = field(default_factory=dict)
    loads: dict[str, SeriesLoad] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    cache_status: str = "miss"

    def symbols(self) -> list[str]:
        """Symbols that returned usable history, in insertion order."""
        return list(self.series)

    def frame(self, *, how: str = "outer") -> pd.DataFrame:
        """All loaded closes as one frame (see :func:`align_series`)."""
        return align_series(self.series, how=how)

    def provenance(self) -> list[Provenance]:
        """Provenance rows for every symbol that loaded."""
        return [load.provenance() for load in self.loads.values()]

    def universe_entries(self, labels: dict[str, tuple[str, str]]) -> list[UniverseEntry]:
        """Universe rows, using ``{symbol: (label, role)}`` for the metadata."""
        entries: list[UniverseEntry] = []
        for symbol, load in self.loads.items():
            label, role = labels.get(symbol, (symbol, "index"))
            entries.append(load.universe_entry(label=label, role=role))
        for symbol, error in self.errors.items():
            if symbol in self.loads:
                continue
            label, role = labels.get(symbol, (symbol, "index"))
            entries.append(
                UniverseEntry(
                    symbol=symbol,
                    label=label,
                    role=role,
                    provider="massive",
                    first_date=None,
                    last_date=None,
                    n_days=0,
                    note=None,
                    error=error,
                )
            )
        return entries


def build_prism_client(**kwargs: Any) -> Any:
    """A ``MarketDataClient`` with the yfinance fallback disabled.

    Yahoo is blocked from the engine's network, and the facade's fallback turns a
    short-but-real Massive history into a hard failure. Prism would rather have
    five honest years than an exception.
    """
    from app.market_data import MarketDataClient

    kwargs.setdefault("fallback_enabled", False)
    return MarketDataClient(**kwargs)


def is_passthrough_symbol(symbol: str) -> bool:
    """True for Massive-namespaced symbols (``X:BTCUSD``, ``C:EURUSD``)."""
    return ":" in str(symbol)


def _history_source(client: Any, symbol: str) -> Any:
    """Pick the object whose ``get_history`` accepts ``symbol`` verbatim."""
    if not is_passthrough_symbol(symbol):
        return client
    provider = getattr(client, "provider", None)
    if provider is not None and hasattr(provider, "get_history"):
        return provider
    return client


def _call_history(client: Any, symbol: str, *, start: date, end: date) -> Any:
    source = _history_source(client, symbol)
    return source.get_history(symbol, start=start, end=end, interval="1d")


def _closes_from_history(result: Any, symbol: str) -> pd.Series:
    """Extract adjusted closes from a ``HistoryResult`` (or a bare frame)."""
    frame = getattr(result, "data", result)
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise PrismDataError(f"{symbol}: provider returned no rows")
    column = "Adj Close" if "Adj Close" in frame.columns else "Close"
    if column not in frame.columns:
        raise PrismDataError(f"{symbol}: history has no close column")
    series = pd.Series(frame[column], copy=True)
    series.index = pd.to_datetime(series.index)
    if getattr(series.index, "tz", None) is not None:
        series.index = series.index.tz_convert(None)
    # Massive stamps a daily bar at 04:00, while the cache round-trip, FRED and
    # Ken French all use plain dates. Left alone, `index <= Timestamp(as_of)`
    # drops the as-of session on a cold build and keeps it on the cached one, so
    # the same ticker built twice returns two different betas. Snap every daily
    # bar to tz-naive midnight, once, here.
    series.index = pd.DatetimeIndex(series.index).normalize()
    series = pd.to_numeric(series, errors="coerce").dropna()
    series = series[series > 0]
    series = series[~series.index.duplicated(keep="last")].sort_index()
    series.name = symbol
    if series.empty:
        raise PrismDataError(f"{symbol}: no positive closes in the returned history")
    return series


def _window_years(years: int) -> list[int]:
    """The retry ladder for one requested window, longest first."""
    requested = max(1, int(years))
    ladder = [requested] + [step for step in FALLBACK_YEARS if step < requested]
    seen: set[int] = set()
    ordered: list[int] = []
    for step in ladder:
        if step not in seen:
            seen.add(step)
            ordered.append(step)
    return ordered


def load_daily(
    client: Any,
    symbol: str,
    *,
    years: int = DEFAULT_YEARS,
    as_of: date | str | None = None,
    cache: PrismCache | None = None,
) -> SeriesLoad:
    """Load up to ``years`` of daily adjusted closes for one symbol.

    Raises :class:`PrismDataError` when no window returns usable rows. The cache
    is keyed by ``(symbol, as-of month)``, so benchmark history is downloaded at
    most once per calendar month per symbol.
    """
    resolved_symbol = str(symbol or "").strip().upper()
    if not resolved_symbol:
        raise PrismDataError("symbol is required")
    end = resolve_as_of(as_of)
    generation = as_of_month(end)

    if cache is not None:
        cached = cache.get_series(resolved_symbol, generation=generation)
        if cached is not None:
            series, meta = cached
            trimmed = series[series.index <= pd.Timestamp(end)]
            # The cache row is keyed by calendar month, so a row written on the
            # 1st would otherwise still be served on the 20th — every close
            # since would silently vanish from beta, entropy and the last price.
            # An entry is only reusable for an as-of date it was already built
            # for; that keeps repeated builds on the same day free (the point of
            # the cache: benchmark history is fetched once, not once per ticker)
            # without ever handing a section a stale price.
            cached_as_of = str(meta.get("as_of") or "")
            fresh = bool(cached_as_of) and cached_as_of >= end.isoformat()
            # Only reuse a cached row that actually covers the requested window.
            # A transient full-window failure can fall back to (and cache) a
            # shorter span; reusing that for a longer request silently truncates
            # every downstream stat's sample. When the cached span is shorter
            # than asked, fall through and re-fetch.
            try:
                cached_years = int(meta.get("requested_years") or 0)
            except (TypeError, ValueError):
                cached_years = 0
            covers_request = cached_years >= int(years)
            if fresh and covers_request and len(trimmed) >= MIN_USABLE_POINTS:
                return SeriesLoad(
                    symbol=resolved_symbol,
                    series=trimmed,
                    provider=str(meta.get("provider") or "massive"),
                    note=str(meta.get("note") or "cached daily closes"),
                    first_date=_iso(trimmed.index[0]),
                    last_date=_iso(trimmed.index[-1]),
                    n_days=int(len(trimmed)),
                    requested_years=int(meta.get("requested_years") or years),
                    as_of=end.isoformat(),
                    cached=True,
                )

    failures: list[str] = []
    for window in _window_years(years):
        start = end - timedelta(days=int(window * CALENDAR_DAYS_PER_YEAR) + 7)
        try:
            result = _call_history(client, resolved_symbol, start=start, end=end)
            series = _closes_from_history(result, resolved_symbol)
        except Exception as exc:  # provider errors are data, not crashes
            failures.append(f"{window}y: {exc}")
            continue
        if len(series) < MIN_USABLE_POINTS:
            failures.append(f"{window}y: only {len(series)} usable closes")
            continue
        provider = str(getattr(result, "provider", "massive") or "massive")
        note = str(getattr(result, "note", "") or "Massive daily aggregates")
        load = SeriesLoad(
            symbol=resolved_symbol,
            series=series,
            provider=provider,
            note=note,
            first_date=_iso(series.index[0]),
            last_date=_iso(series.index[-1]),
            n_days=int(len(series)),
            requested_years=int(window),
            as_of=end.isoformat(),
        )
        if cache is not None:
            cache.set_series(
                resolved_symbol,
                series,
                meta={
                    "provider": provider,
                    "note": note,
                    "requested_years": int(window),
                    "first_date": load.first_date,
                    "last_date": load.last_date,
                    "as_of": end.isoformat(),
                },
                generation=generation,
            )
        return load
    detail = "; ".join(failures) or "no attempts"
    raise PrismDataError(f"{resolved_symbol}: no daily history ({detail})")


def daily_closes(
    client: Any,
    symbol: str,
    *,
    years: int = DEFAULT_YEARS,
    as_of: date | str | None = None,
    cache: PrismCache | None = None,
) -> pd.Series:
    """:func:`load_daily` reduced to just the series, for callers that only want prices."""
    return load_daily(client, symbol, years=years, as_of=as_of, cache=cache).series


def load_universe(
    client: Any,
    symbols: Sequence[str],
    *,
    years: int = DEFAULT_YEARS,
    as_of: date | str | None = None,
    cache: PrismCache | None = None,
    max_workers: int | None = None,
) -> UniverseData:
    """Load many symbols concurrently; a failing symbol becomes an error entry."""
    ordered: list[str] = []
    for symbol in symbols:
        cleaned = str(symbol or "").strip().upper()
        if cleaned and cleaned not in ordered:
            ordered.append(cleaned)
    if not ordered:
        return UniverseData()

    workers = max(1, int(max_workers or _env_int("PRISM_MAX_WORKERS", DEFAULT_MAX_WORKERS)))
    result = UniverseData()

    def _load(symbol: str) -> tuple[str, SeriesLoad | None, str | None]:
        try:
            return symbol, load_daily(
                client, symbol, years=years, as_of=as_of, cache=cache
            ), None
        except Exception as exc:
            return symbol, None, str(exc)

    with ThreadPoolExecutor(max_workers=min(workers, len(ordered))) as pool:
        loaded = list(pool.map(_load, ordered))

    by_symbol = {symbol: (load, error) for symbol, load, error in loaded}
    for symbol in ordered:
        load, error = by_symbol[symbol]
        if load is None:
            result.errors[symbol] = error or "unknown error"
            continue
        result.loads[symbol] = load
        result.series[symbol] = load.series
    result.cache_status = cache.status() if cache is not None else "disabled"
    return result


def align_series(
    series_map: dict[str, pd.Series],
    *,
    how: str = "outer",
    min_points: int = 1,
) -> pd.DataFrame:
    """Combine per-symbol series into one date-indexed frame.

    ``how="inner"`` keeps only dates every symbol traded (the frame used for
    covariance and cosine similarity, where a ragged panel would bias the
    result); ``how="outer"`` keeps every date and leaves gaps as ``NaN``.
    """
    usable = {
        symbol: series.dropna()
        for symbol, series in series_map.items()
        if isinstance(series, pd.Series) and len(series.dropna()) >= min_points
    }
    if not usable:
        return pd.DataFrame()
    frame = pd.concat(usable.values(), axis=1, join="inner" if how == "inner" else "outer")
    frame.columns = list(usable)
    frame = frame.sort_index()
    frame.index = pd.to_datetime(frame.index)
    return frame


def to_returns(
    data: pd.Series | pd.DataFrame,
    *,
    log: bool = False,
) -> pd.Series | pd.DataFrame:
    """Simple (default) or log daily returns, with the first row dropped."""
    returns = np.log(data / data.shift(1)) if log else data.pct_change()
    return returns.iloc[1:]


def trailing_window(
    data: pd.Series | pd.DataFrame,
    window: str | int,
    *,
    as_of: date | str | None = None,
) -> pd.Series | pd.DataFrame:
    """Slice the last ``window`` trading rows (``"1y"``/``"6m"``/… or a count)."""
    if isinstance(window, str):
        days = WINDOW_DAYS.get(window)
        if days is None:
            raise ValueError(f"unknown window: {window}")
    else:
        days = int(window)
    frame = data
    if as_of is not None:
        cutoff = pd.Timestamp(resolve_as_of(as_of))
        frame = frame[frame.index <= cutoff]
    if days <= 0 or len(frame) <= days:
        return frame
    return frame.iloc[-days:]


def common_window(
    series_map: dict[str, pd.Series],
    window: str | int,
    *,
    as_of: date | str | None = None,
    min_symbols: int = 2,
) -> pd.DataFrame:
    """Aligned daily returns over one trailing window, on shared dates only."""
    frame = align_series(series_map, how="outer")
    if frame.empty:
        return pd.DataFrame()
    windowed = trailing_window(frame, window, as_of=as_of)
    assert isinstance(windowed, pd.DataFrame)
    returns = windowed.pct_change().iloc[1:]
    returns = returns.dropna(axis=1, how="all").dropna()
    if returns.shape[1] < min_symbols:
        return pd.DataFrame()
    return returns


def coverage_report(data: UniverseData) -> dict[str, Any]:
    """A compact per-symbol coverage summary for ``meta.source_status``."""
    return {
        "loaded": len(data.loads),
        "failed": len(data.errors),
        "cache": data.cache_status,
        "symbols": {
            symbol: {
                "first_date": load.first_date,
                "last_date": load.last_date,
                "n_days": load.n_days,
                "years": load.coverage_years,
                "provider": load.provider,
                "cached": load.cached,
            }
            for symbol, load in data.loads.items()
        },
        "errors": dict(data.errors),
    }


def finite(value: Any) -> float | None:
    """Coerce to a JSON-safe float, mapping NaN/inf and non-numbers to ``None``."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def finite_list(values: Iterable[Any]) -> list[float | None]:
    """:func:`finite` over an iterable."""
    return [finite(value) for value in values]


def _iso(stamp: Any) -> str:
    return pd.Timestamp(stamp).date().isoformat()


def resolve_as_of(as_of: date | str | None) -> date:
    """Normalise ``None``/``date``/``datetime``/ISO string to a UTC calendar date."""
    if as_of is None:
        return datetime.now(UTC).date()
    if isinstance(as_of, datetime):
        return as_of.date()
    if isinstance(as_of, date):
        return as_of
    return date.fromisoformat(str(as_of)[:10])


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


#: Backwards-compatible private alias (some callers imported this before it was public).
_resolve_as_of = resolve_as_of
