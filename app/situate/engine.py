"""Situate engine: orchestrate S1-S4 into one packet, then odds, scenarios, memo.

The engine is the only module that touches the network for every source. It fans
the independent sections out across a small thread pool, wraps each in its own
``try/except`` so one failing source can never sink the whole build (the reason
lands in ``meta.errors`` instead), and persists both every module result and the
full packet through Prism's cache/store plumbing — rooted under a ``situate``
sub-directory so a Situate packet never overwrites a Prism one for the same
ticker and date.

Each S1-S4 module is imported lazily inside its guard, so a module that is not
present in this checkout yet degrades to a ``None`` section with a stated reason
rather than an import error — the guaranteed ship state (SPEC §11) is
``base_rates`` + ``implied`` + a memo, and the engine reaches it even when the
richer modules are absent.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from datetime import date
from typing import Any

import pandas as pd

from app.situate.contract import (
    ENGINE_VERSION,
    empty_packet,
    record_error,
    record_source,
    record_timing,
    record_unavailable,
    record_version,
    set_section,
)

DEFAULT_YEARS = 12
DEFAULT_MAX_WORKERS = 4

#: Base panel symbols the exposure/state/base-rate modules need (SPEC 5.1/5.2).
BASE_PANEL_SYMBOLS: tuple[str, ...] = ("SPY", "IWM", "UUP", "FXY", "USO", "GLD")
#: FRED series the exposure basket and state context read (levels; the modules
#: take their own single first difference).
MACRO_SERIES: tuple[str, ...] = ("DGS10", "DGS2", "BAMLH0A0HYM2", "VIXCLS")

#: Cap on the cross-sectional stack universe so the extra panel load stays bounded.
STACK_UNIVERSE_LIMIT = 60


class SituateEngineError(RuntimeError):
    """Raised only when the packet cannot be started at all (e.g. no ticker)."""


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _finitize(obj: Any) -> Any:
    """Recursively replace non-finite floats (NaN/Inf) with ``None`` in place.

    Modules use ``float('nan')`` as an internal "undefined" sentinel (e.g. an
    ablation IC on a horizon with too little data). A NaN is not a real number
    and must never surface as one: it is invalid JSON, breaks the Supabase and
    JS parsers, and would read as a fabricated value. This normalises every such
    sentinel to the contract's ``null`` before the packet is persisted or
    returned, so the packet is always strict-JSON compliant.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        for key, value in list(obj.items()):
            obj[key] = _finitize(value)
        return obj
    if isinstance(obj, list):
        for index, value in enumerate(obj):
            obj[index] = _finitize(value)
        return obj
    if isinstance(obj, tuple):
        return [_finitize(value) for value in obj]
    return obj


@contextmanager
def _timed(packet: dict[str, Any], name: str) -> Any:
    started = time.perf_counter()
    try:
        yield
    finally:
        record_timing(packet, name, (time.perf_counter() - started) * 1000.0)


def _guard(packet: dict[str, Any], section: str, builder: Callable[[], Any]) -> Any:
    """Run one section builder; a failure sets the section to ``None`` + a reason."""
    with _timed(packet, section):
        try:
            value = builder()
        except Exception as exc:  # noqa: BLE001 - one section must never sink the build
            set_section(packet, section, None, error=f"{type(exc).__name__}: {exc}")
            return None
        if value is None:
            set_section(packet, section, None, error="builder returned nothing")
            return None
        set_section(packet, section, value)
        packet["meta"].setdefault("source_status", {})[section] = "available"
        _record_section_errors(packet, section, value)
        if isinstance(value, Mapping) and value.get("version"):
            record_version(packet, section, str(value["version"]))
        return value


def _commit_section(packet: dict[str, Any], section: str, value: Any, error: str | None) -> Any:
    """Write an already-computed section (from the pool) with uniform bookkeeping."""
    if error is not None:
        set_section(packet, section, None, error=error)
        return None
    if value is None:
        set_section(packet, section, None, error="builder returned nothing")
        return None
    set_section(packet, section, value)
    packet["meta"].setdefault("source_status", {})[section] = "available"
    _record_section_errors(packet, section, value)
    if isinstance(value, Mapping) and value.get("version"):
        record_version(packet, section, str(value["version"]))
    return value


def _record_section_errors(packet: dict[str, Any], section: str, value: Any) -> None:
    """Lift a section's own ``errors`` list into ``meta.unavailable``."""
    if not isinstance(value, Mapping):
        return
    for reason in value.get("errors") or []:
        if isinstance(reason, str) and reason.strip():
            record_unavailable(packet, section, reason)
        elif isinstance(reason, Mapping):
            record_unavailable(
                packet,
                str(reason.get("source") or section),
                str(reason.get("error") or reason.get("reason") or reason),
            )
    for row in value.get("unavailable") or []:
        if isinstance(row, Mapping):
            record_unavailable(
                packet,
                str(row.get("source") or section),
                str(row.get("reason") or row.get("error") or row),
            )


#: Max days the live option snapshot may post-date ``as_of`` before the implied
#: section stops being point-in-time (weekend/holiday gap tolerance). Massive has
#: no historical option chain, so a materially older ``as_of`` cannot be priced
#: without leaking future data — implied then degrades to null + reason.
IMPLIED_SNAPSHOT_TOLERANCE_DAYS = 4


def _guard_implied_snapshot(packet: dict[str, Any], as_of: str) -> None:
    """Null the implied section when its live snapshot post-dates ``as_of``.

    The options snapshot is always fetched as-of *now*; that is correct for a
    live build (``as_of`` is the latest close) but for a historical rebuild it
    would attach a future chain to a past date. To keep the packet honest and
    the lookahead test clean, replace such an implied section with null and a
    stated reason rather than presenting future option data as point-in-time.
    """
    section = packet.get("implied")
    if not isinstance(section, Mapping):
        return
    snapshot_ts = section.get("snapshot_ts")
    if not snapshot_ts:
        return
    try:
        snap_date = date.fromisoformat(str(snapshot_ts)[:10])
        as_of_date = date.fromisoformat(str(as_of)[:10])
    except (TypeError, ValueError):
        return
    lag = (snap_date - as_of_date).days
    if lag <= IMPLIED_SNAPSHOT_TOLERANCE_DAYS:
        return
    # Only future *data* leaks. When every horizon is already null (the chain was
    # unavailable), there is nothing to leak — keep the honest empty section (with
    # its per-horizon reasons) rather than collapsing the whole block to null.
    by_horizon = section.get("by_horizon")
    has_data = isinstance(by_horizon, Mapping) and any(
        block is not None for block in by_horizon.values()
    )
    if not has_data:
        note = (
            f"option snapshot dated {snap_date.isoformat()} post-dates as_of "
            f"{as_of_date.isoformat()} by {lag}d; no historical chain available"
        )
        record_unavailable(packet, "implied", note)
        return
    reason = (
        f"option snapshot dated {snap_date.isoformat()} post-dates as_of "
        f"{as_of_date.isoformat()} by {lag}d; Massive has no historical option "
        "chain, so a point-in-time implied distribution is unavailable before "
        "the snapshot date"
    )
    set_section(packet, "implied", None, error=reason)
    record_unavailable(packet, "implied", reason)


# --------------------------------------------------------------------------
# Store (rooted under a situate/ sub-directory so it never collides with Prism)
# --------------------------------------------------------------------------


def _situate_store(store: Any | None = None) -> Any:
    if store is not None:
        return store
    from pathlib import Path

    from app.prism.store import PrismStore, store_dir_from_env

    return PrismStore.from_env(base_dir=Path(store_dir_from_env()) / "situate")


def get_situate_packet(
    ticker: str, as_of: date | str | None = None, *, store: Any | None = None
) -> dict[str, Any] | None:
    """Read the latest stored Situate packet for ``ticker`` (``None`` if none)."""
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        return None
    return _situate_store(store).load_packet(symbol, as_of)


# --------------------------------------------------------------------------
# Profile + ETF resolution
# --------------------------------------------------------------------------


def _resolve_etfs(ticker: str, profile: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """``(sector_etf, industry_etf)`` for a ticker: the broad sector sleeve and a
    narrower thematic ETF when one applies (SOXX for semis, XBI for biotech...)."""
    from app.prism.universe import industry_etfs, sector_etf
    from app.situate import peers

    sector = profile.get("sector")
    industry = profile.get("industry")
    description = profile.get("description")
    resolved_sector = (
        peers.industry_etf_of(ticker) or sector_etf(sector) or sector_etf(industry)
    )
    thematic = industry_etfs(industry, description)
    industry_etf = next((etf for etf in thematic if etf and etf != resolved_sector), None)
    return resolved_sector, industry_etf


# --------------------------------------------------------------------------
# Per-module persistence
# --------------------------------------------------------------------------


#: Canonical provider metadata for the external services Situate consults. Each
#: row is recorded in ``packet["sources"]`` only when that service was actually
#: reachable in this build, so the list is real provenance, never fabricated.
_PROVIDER_META: tuple[tuple[str, str, str], ...] = (
    ("Massive", "https://api.joinmassive.com", "daily closes, options chain, fundamentals"),
    ("FRED", "https://fred.stlouisfed.org", "macro series (rates, credit, VIX) levels"),
    (
        "Ken French Data Library",
        "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html",
        "monthly Fama-French + momentum factor returns",
    ),
    ("SEC EDGAR", "https://www.sec.gov", "10-K / 10-Q filings"),
    ("Exa", "https://exa.ai", "news search"),
    ("Anthropic", "https://api.anthropic.com", "memo narrative + filing-diff scoring"),
)


def _aggregate_sources(
    packet: dict[str, Any],
    *,
    used: Mapping[str, bool],
) -> None:
    """Populate the top-level ``sources`` provenance list.

    Two contributions, both drawn from what actually happened in this build:
    (1) the concrete per-row provenance a section already recorded (the text
    section carries real filing/news URLs with fetch timestamps), and (2) one
    provider-level row for each external service that was genuinely reachable.
    Nothing is invented — a provider that was not consulted is omitted.
    """
    fetched_at = str(packet.get("generated_at") or "")
    for section_name in ("text",):
        section = packet.get(section_name)
        if isinstance(section, Mapping):
            for row in section.get("sources") or []:
                if isinstance(row, Mapping) and row.get("provider"):
                    record_source(
                        packet,
                        {
                            key: row.get(key)
                            for key in ("provider", "url", "symbol", "fetched_at", "note")
                            if row.get(key) is not None
                        },
                    )
    for provider, url, note in _PROVIDER_META:
        if used.get(provider):
            record_source(
                packet,
                {"provider": provider, "url": url, "fetched_at": fetched_at, "note": note},
            )


def _persist_module(cache: Any, ticker: str, as_of: str, module: str, value: Any) -> None:
    if cache is None or value is None:
        return
    # A failed module cache write is never fatal to the build.
    with suppress(Exception):
        cache.set(
            f"situate_{module}",
            f"{ticker}:{as_of}",
            {"module": module, "ticker": ticker, "as_of": as_of, "result": value},
            generation=as_of,
        )


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


def build_situate_packet(
    client: Any,
    ticker: str,
    *,
    sec_client: Any | None = None,
    exa_client: Any | None = None,
    text_generator: Any | None = None,
    as_of: date | str | None = None,
    include_memo: bool = True,
    force: bool = False,
    cache: Any | None = None,
    store: Any | None = None,
    fred_client: Any | None = None,
    years: int = DEFAULT_YEARS,
    api_key: str | None = None,
    text_model: str | None = None,
    persist: bool = True,
    include_stack: bool = True,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict[str, Any]:
    """Build one full Situate packet for ``ticker``.

    ``force`` bypasses today's stored packet and rebuilds. ``include_memo=False``
    returns everything except the narrative. The build never raises for a data
    outage: every gap is recorded in ``meta`` and the packet still validates.
    """
    from app.prism.data import resolve_as_of

    symbol = str(ticker or "").strip().upper()
    if not symbol:
        raise SituateEngineError("ticker is required")

    resolved_as_of = resolve_as_of(as_of).isoformat()
    situate_store = _situate_store(store) if persist else None

    if not force and situate_store is not None:
        existing = situate_store.load_packet(symbol, resolved_as_of)
        if existing is not None:
            existing.setdefault("meta", {}).setdefault("cache", {})["packet"] = "hit"
            return existing

    packet = empty_packet(symbol, as_of=resolved_as_of)
    packet["engine_version"] = ENGINE_VERSION
    packet["meta"]["cache"] = {"packet": "miss"}

    prism_cache = cache
    if prism_cache is None:
        try:
            from app.prism.cache import PrismCache

            prism_cache = PrismCache.from_env()
        except Exception as exc:  # noqa: BLE001
            record_error(packet, "cache", f"cache unavailable: {exc}")
            prism_cache = None

    if fred_client is None:
        try:
            from app.prism.macro import fred_client_from_env

            fred_client = fred_client_from_env()
        except Exception as exc:  # noqa: BLE001
            record_unavailable(packet, "fred", f"FRED client unavailable: {exc}")

    if text_generator is None and api_key:
        try:
            from app.anthropic import AnthropicTextClient

            text_generator = AnthropicTextClient(api_key=api_key, model=text_model)
        except Exception as exc:  # noqa: BLE001
            record_unavailable(packet, "anthropic", f"text generator unavailable: {exc}")
    if text_generator is None:
        record_unavailable(
            packet,
            "anthropic",
            "no text generator or API key; narrative sections use the deterministic template",
        )

    # -------------------------------------------------------------- profile
    from app.prism.engine import build_profile

    profile = _guard(packet, "profile", lambda: build_profile(client, symbol)) or {}
    sector_etf, industry_etf = _resolve_etfs(symbol, profile)
    benchmark_etf = industry_etf or sector_etf
    packet["meta"].setdefault("notes", []).append(
        f"sector_etf={sector_etf}, industry_etf={industry_etf}, benchmark_etf={benchmark_etf}"
    )

    # ----------------------------------------------------------------- panel
    panel = None
    macro_monthly = None
    factors_monthly = None
    with _timed(packet, "panel"):
        try:
            from app.situate.panel import load_macro_monthly, load_panel

            symbols = [symbol, *BASE_PANEL_SYMBOLS]
            for etf in (sector_etf, industry_etf):
                if etf and etf not in symbols:
                    symbols.append(etf)
            panel = load_panel(
                client, symbols, as_of=resolved_as_of, years=years, cache=prism_cache
            )
            for sym, reason in (panel.errors or {}).items():
                record_unavailable(packet, f"panel.{sym}", str(reason))
        except Exception as exc:  # noqa: BLE001
            record_error(packet, "panel", f"panel load failed: {exc}")
            panel = None
        if panel is not None and fred_client is not None:
            try:
                macro_monthly = load_macro_monthly(
                    fred_client, MACRO_SERIES, as_of=resolved_as_of, cache=prism_cache
                )
            except Exception as exc:  # noqa: BLE001
                record_unavailable(packet, "macro", f"macro panel unavailable: {exc}")
        try:
            from app.situate.factors_data import load_ken_french_monthly

            factors_monthly, factor_prov = load_ken_french_monthly(as_of=resolved_as_of)
            if factor_prov.get("error"):
                record_unavailable(packet, "factors_data", str(factor_prov["error"]))
        except Exception as exc:  # noqa: BLE001
            record_unavailable(packet, "factors_data", f"Ken French factors unavailable: {exc}")

    close = (
        panel.daily_close(symbol)
        if panel is not None and panel.has(symbol)
        else pd.Series(dtype="float64")
    )
    current_price = float(close.iloc[-1]) if len(close) else None

    # --------------------------------------------------- section builders
    def _build_exposure() -> Any:
        if panel is None:
            raise SituateEngineError("panel unavailable")
        from app.situate.exposure import build_exposure_section

        return build_exposure_section(
            panel,
            ticker=symbol,
            macro_monthly=macro_monthly,
            factors_monthly=factors_monthly,
            sector_etf=sector_etf,
            industry_etf=industry_etf,
        )

    def _build_state() -> Any:
        if panel is None:
            raise SituateEngineError("panel unavailable")
        from app.situate.state import state_section

        return state_section(
            panel, ticker=symbol, fred=fred_client, cache=prism_cache, as_of=resolved_as_of
        )

    def _build_base_rates() -> Any:
        if panel is None:
            raise SituateEngineError("panel unavailable")
        from app.situate.base_rates import build_base_rates

        close_ind = (
            panel.daily_close(benchmark_etf)
            if benchmark_etf and panel.has(benchmark_etf)
            else None
        )
        return build_base_rates(close, close_ind)

    def _build_fundamentals() -> Any:
        from app.situate.fundamentals import build_fundamentals_section

        return build_fundamentals_section(
            client,
            symbol,
            prices=close if len(close) else None,
            as_of=resolved_as_of,
            current_price=current_price,
        )

    def _build_text() -> Any:
        from app.situate.text import build_text_section

        return build_text_section(
            symbol,
            sec_client=sec_client,
            exa_client=exa_client,
            market_client=client,
            text_generator=text_generator,
            company_name=profile.get("name"),
            as_of=resolved_as_of,
        )

    # ----------------------------------------- fan out the independent group
    builders: dict[str, Callable[[], Any]] = {
        "exposure": _build_exposure,
        "state": _build_state,
        "base_rates": _build_base_rates,
        "fundamentals": _build_fundamentals,
        "text": _build_text,
    }

    def _run(name: str, fn: Callable[[], Any]) -> tuple[str, Any, str | None, float]:
        started = time.perf_counter()
        try:
            value = fn()
            error = None
        except Exception as exc:  # noqa: BLE001 - one section must never sink the build
            value, error = None, f"{type(exc).__name__}: {exc}"
        return name, value, error, (time.perf_counter() - started) * 1000.0

    with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), 4))) as pool:
        outcomes = list(pool.map(lambda item: _run(*item), builders.items()))

    for name, value, error, elapsed_ms in outcomes:
        record_timing(packet, name, elapsed_ms)
        _commit_section(packet, name, value, error)

    base_rates_section = (
        packet.get("base_rates") if isinstance(packet.get("base_rates"), Mapping) else {}
    )

    # ------------------------------------------------------------- implied
    def _build_implied() -> Any:
        from app.situate.base_rates import conditional_iqr_by_horizon, shrunk_median_by_horizon
        from app.situate.implied import build_implied

        hist_iqr = conditional_iqr_by_horizon(base_rates_section) if base_rates_section else None
        shrunk_med = shrunk_median_by_horizon(base_rates_section) if base_rates_section else None
        return build_implied(
            client,
            symbol,
            as_of=resolved_as_of,
            spot=current_price,
            hist_cond_iqr=hist_iqr,
            shrunk_base_median=shrunk_med,
        )

    _guard(packet, "implied", _build_implied)
    _guard_implied_snapshot(packet, resolved_as_of)
    implied_section = packet.get("implied") if isinstance(packet.get("implied"), Mapping) else None

    # -------------------------------------------------------------- levels
    def _build_levels() -> Any:
        from app.situate.levels import build_levels

        history = client.get_history(
            symbol, start=date(2000, 1, 1), end=resolve_as_of(resolved_as_of), interval="1d"
        ) if hasattr(client, "get_history") else close
        return build_levels(
            history if history is not None else close,
            implied=implied_section,
            current_price=current_price,
            profile=profile,
        )

    _guard(packet, "levels", _build_levels)

    # --------------------------------------------------------------- stack
    if include_stack:

        def _build_stack() -> Any:
            from app.situate import peers as peers_mod
            from app.situate.panel import load_panel as _load_panel
            from app.situate.stack import build_stack

            universe = peers_mod.universe_for(symbol, limit=STACK_UNIVERSE_LIMIT)
            etf_of = peers_mod.etf_map(universe)
            wanted = sorted({*universe, *(v for v in etf_of.values() if v)})
            stack_panel = _load_panel(
                client, wanted, as_of=resolved_as_of, years=years, cache=prism_cache
            )
            return build_stack(
                stack_panel, symbol, universe=universe, etf_of=etf_of, as_of=resolved_as_of
            )

        _guard(packet, "stack", _build_stack)
    else:
        set_section(packet, "stack", None, error="stack disabled for this build")

    # ------------------------------------------------- odds + scenarios
    def _build_odds() -> Any:
        from app.situate.odds import build_odds

        return build_odds(packet)

    odds = _guard(packet, "odds", _build_odds)

    def _build_scenarios() -> Any:
        from app.situate.odds import build_scenarios

        return build_scenarios(packet, odds)

    _guard(packet, "scenarios", _build_scenarios)

    # ---------------------------------------------------------------- memo
    if include_memo:

        def _build_memo() -> Any:
            from app.situate.memo import build_memo

            return build_memo(
                packet, text_generator=text_generator, api_key=api_key, text_model=text_model
            )

        _guard(packet, "memo", _build_memo)
    else:
        set_section(packet, "memo", None, error="include_memo=False")

    # --------------------------------------------------- persist + finalise
    # Real provider provenance: which external services this build reached.
    status = packet["meta"].get("source_status", {})
    market_ok = any(
        status.get(name) == "available"
        for name in ("profile", "exposure", "state", "base_rates", "implied", "levels")
    )
    _aggregate_sources(
        packet,
        used={
            "Massive": client is not None and market_ok,
            "FRED": fred_client is not None and macro_monthly is not None,
            "Ken French Data Library": factors_monthly is not None,
            "SEC EDGAR": sec_client is not None,
            "Exa": exa_client is not None,
            "Anthropic": text_generator is not None,
        },
    )

    # Normalise any NaN/Inf sentinel to the contract's null before the packet is
    # persisted (Supabase/JSON reject non-finite floats) or handed back.
    _finitize(packet)

    if prism_cache is not None:
        _modules = (
            "exposure", "state", "base_rates", "implied",
            "fundamentals", "text", "levels", "stack",
        )
        for module in _modules:
            _persist_module(prism_cache, symbol, resolved_as_of, module, packet.get(module))
        packet["meta"]["cache"]["hits"] = int(getattr(prism_cache, "hits", 0))
        packet["meta"]["cache"]["misses"] = int(getattr(prism_cache, "misses", 0))

    if persist and situate_store is not None:
        try:
            stored = situate_store.save_packet(packet)
            packet["meta"]["stored"] = stored
            for reason in (stored or {}).get("errors") or []:
                record_unavailable(packet, "store", str(reason))
        except Exception as exc:  # noqa: BLE001 - a failed write must not lose the packet
            record_error(packet, "store", f"could not persist packet: {exc}")

    return packet


# --------------------------------------------------------------------------
# Bounded summary projection
# --------------------------------------------------------------------------


def _sub_cell(state: Mapping[str, Any], key: str) -> Any:
    child = state.get(key) if isinstance(state, Mapping) else None
    return child.get("cell") if isinstance(child, Mapping) else None


def situate_summary(packet: Mapping[str, Any], *, max_events: int = 5) -> dict[str, Any]:
    """The bounded projection an agent or a proxy should receive."""
    def _sec(name: str) -> Mapping[str, Any]:
        value = packet.get(name)
        return value if isinstance(value, Mapping) else {}

    memo = _sec("memo")
    profile = _sec("profile")
    exposure = _sec("exposure")
    state = _sec("state")
    odds = _sec("odds")
    meta = _sec("meta")

    odds_by_h = odds.get("by_horizon") if isinstance(odds.get("by_horizon"), Mapping) else {}
    odds_view: dict[str, Any] = {}
    for h, block in (odds_by_h or {}).items():
        if isinstance(block, Mapping):
            odds_view[h] = {
                "source": block.get("source"),
                "p_up": block.get("p_up"),
                "q50": (block.get("quantiles") or {}).get("q50")
                if isinstance(block.get("quantiles"), Mapping)
                else None,
            }

    text = _sec("text")
    raw_events = text.get("events")
    events = raw_events if isinstance(raw_events, list) else []

    return {
        "ticker": packet.get("ticker"),
        "as_of": packet.get("as_of"),
        "generated_at": packet.get("generated_at"),
        "engine": packet.get("engine"),
        "engine_version": packet.get("engine_version"),
        "name": profile.get("name"),
        "sector": profile.get("sector"),
        "industry": profile.get("industry"),
        "posture": memo.get("posture"),
        "one_line": (memo.get("posture") or {}).get("one_line"),
        "falsifiers": memo.get("falsifiers") or [],
        "whats_priced_in": memo.get("whats_priced_in") or [],
        "zones": memo.get("zones") or {},
        "exposure": {
            "r2": exposure.get("r2"),
            "idiosyncratic_share": exposure.get("idiosyncratic_share"),
            "betas": exposure.get("betas") or {},
        },
        "state": {
            "spy_cell": _sub_cell(state, "spy"),
            "ticker_cell": _sub_cell(state, "ticker"),
        },
        "odds": odds_view,
        "stack_published": bool(_sec("stack").get("published")),
        "events": [
            {"date": e.get("date"), "headline": e.get("headline"), "sentiment": e.get("sentiment")}
            for e in events[: max(1, int(max_events))]
            if isinstance(e, Mapping)
        ],
        "memo_excerpt": str(memo.get("text") or "")[:1500],
        "unavailable_sections": [
            name
            for name in (
                "profile", "exposure", "state", "base_rates", "implied",
                "fundamentals", "text", "levels", "stack", "odds", "scenarios", "memo",
            )
            if packet.get(name) is None
        ],
        "errors": meta.get("errors") or [],
        "disclaimer": "Research only. Not investment advice; no price target; no order was placed.",
    }
