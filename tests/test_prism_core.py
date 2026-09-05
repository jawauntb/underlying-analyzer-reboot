"""Offline, deterministic tests for the Prism engine core (workstream W1).

Every non-live test builds its own synthetic series with a seeded RNG or an
exactly known monthly pattern, so an assertion failure means the maths changed,
not that the market moved. The live smoke tests at the bottom only run with
``PRISM_LIVE=1`` and real keys in the environment.
"""

from __future__ import annotations

import json
import math
import os
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.prism.cache import (
    PrismCache,
    SupabaseSeriesCache,
    as_of_month,
    payload_to_series,
    safe_key,
    series_to_payload,
)
from app.prism.contract import (
    ENGINE_VERSION,
    PACKET_KEYS,
    empty_macro_series,
    empty_packet,
    empty_seasonal_stats,
    month_label,
    record_error,
    record_timing,
    record_unavailable,
    set_section,
    validate_packet,
)
from app.prism.data import (
    PrismDataError,
    align_series,
    common_window,
    daily_closes,
    finite,
    is_passthrough_symbol,
    load_daily,
    load_universe,
    resolve_as_of,
    to_returns,
    trailing_window,
)
from app.prism.macro import (
    FredClient,
    FredError,
    build_macro_section,
    build_macro_series,
    curve_shape,
    monthly_points,
    value_as_of,
)
from app.prism.relational import (
    build_relational_section,
    cosine_similarity_matrix,
    covariance_matrix,
    gauge_fix,
    impact_weights,
    kinematics,
    ols_beta,
    relative_moving_average,
    rolling_beta,
    trend_label,
)
from app.prism.seasonality import (
    build_seasonality_section,
    month_end_closes,
    monthly_returns,
    seasonal_stats,
    seasonal_trend,
    this_month_stats,
)
from app.prism.universe import (
    BENCHMARKS_BY_SYMBOL,
    COVERAGE_NOTES,
    SYMBOL_REMAP,
    benchmark_symbols,
    fred_series_ids,
    industry_etfs,
    normalize_symbol,
    related_etfs,
    remap_note,
    resolve_universe,
    sector_etf,
    universe_symbols,
)

AS_OF = date(2026, 9, 1)


# --------------------------------------------------------------------------- helpers


def business_days(start: str, end: str) -> pd.DatetimeIndex:
    return pd.bdate_range(start=start, end=end)


def step_series(
    *,
    start: str = "2014-01-01",
    end: str = "2026-08-31",
    base: float = 100.0,
    monthly_factor: Any,
    name: str = "TEST",
) -> pd.Series:
    """Daily closes that are constant inside each month.

    Because every intra-month observation is identical, the return of calendar
    month ``m`` is exactly ``monthly_factor(year, month) - 1``, which makes the
    seasonality assertions exact rather than approximate.
    """
    index = business_days(start, end)
    periods = index.to_period("M")
    level = base
    values: list[float] = []
    previous = None
    for period in periods:
        if period != previous:
            level *= float(monthly_factor(period.year, period.month))
            previous = period
        values.append(level)
    return pd.Series(values, index=index, name=name, dtype="float64")


def geometric_series(
    returns: np.ndarray, *, index: pd.DatetimeIndex, base: float = 100.0, name: str = "SYM"
) -> pd.Series:
    levels = base * np.cumprod(1.0 + returns)
    return pd.Series(levels, index=index, name=name, dtype="float64")


class FakeHistory:
    """Stands in for ``app.market_data.HistoryResult``."""

    def __init__(self, ticker: str, frame: pd.DataFrame, provider: str = "massive") -> None:
        self.ticker = ticker
        self.data = frame
        self.provider = provider
        self.note = "fake provider"
        self.interval = "1d"


class FakeProvider:
    """Accepts Massive-namespaced symbols verbatim, like ``MassiveProvider``."""

    name = "massive"

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames = frames
        self.calls: list[dict[str, Any]] = []

    def get_history(self, ticker: str, **kwargs: Any) -> FakeHistory:
        self.calls.append({"ticker": ticker, **kwargs})
        if ticker not in self.frames:
            raise RuntimeError(f"no fake history for {ticker}")
        return FakeHistory(ticker, self.frames[ticker])


class FakeClient:
    """Stands in for ``MarketDataClient``: rejects ``:`` like ``clean_ticker`` does."""

    def __init__(
        self,
        frames: dict[str, pd.DataFrame],
        *,
        fail_windows: dict[str, int] | None = None,
    ) -> None:
        self.provider = FakeProvider(frames)
        self.frames = frames
        self.calls: list[dict[str, Any]] = []
        # symbol -> the shortest window (in days) that is allowed to succeed
        self.fail_windows = fail_windows or {}

    def get_history(self, ticker: str, **kwargs: Any) -> FakeHistory:
        if ":" in ticker:
            raise ValueError("Ticker contains unsupported characters")
        self.calls.append({"ticker": ticker, **kwargs})
        limit = self.fail_windows.get(ticker)
        if limit is not None:
            start = kwargs.get("start")
            end = kwargs.get("end")
            if start is not None and end is not None and (end - start).days > limit:
                raise RuntimeError(f"{ticker}: history window too long")
        if ticker not in self.frames:
            raise RuntimeError(f"no fake history for {ticker}")
        return FakeHistory(ticker, self.frames[ticker])


def ohlcv(series: pd.Series) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": series.to_numpy(),
            "High": series.to_numpy() * 1.01,
            "Low": series.to_numpy() * 0.99,
            "Close": series.to_numpy(),
            "Adj Close": series.to_numpy(),
            "Volume": np.full(len(series), 1_000_000.0),
        },
        index=series.index,
    )


class FakeResponse:
    def __init__(self, payload: Any, *, status_code: int = 200, text: str | None = None) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)

    def json(self) -> Any:
        return self._payload


class FakeFredSession:
    """Serves FRED observation payloads from an in-memory series map."""

    def __init__(self, series: dict[str, pd.Series], *, fail: set[str] | None = None) -> None:
        self.series = series
        self.fail = fail or set()
        self.requests: list[dict[str, Any]] = []

    def get(self, url: str, *, params: dict[str, Any], timeout: float) -> FakeResponse:
        self.requests.append({"url": url, "params": dict(params), "timeout": timeout})
        series_id = str(params.get("series_id"))
        if series_id in self.fail:
            return FakeResponse({"error": "boom"}, status_code=400)
        data = self.series.get(series_id)
        if data is None:
            return FakeResponse({"observations": []})
        observations = [
            {"date": stamp.date().isoformat(), "value": f"{value:.4f}"}
            for stamp, value in data.items()
        ]
        # FRED writes gaps as "."; make sure the parser drops them.
        observations.insert(0, {"date": "1990-01-01", "value": "."})
        return FakeResponse({"observations": observations})


# --------------------------------------------------------------------------- contract


def test_empty_packet_has_every_contract_key() -> None:
    packet = empty_packet("nvda", as_of=AS_OF)
    assert packet["ticker"] == "NVDA"
    assert packet["as_of"] == "2026-09-01"
    assert packet["engine_version"] == ENGINE_VERSION
    assert packet["name"] == "Prism"
    for key in PACKET_KEYS:
        assert key in packet, key
    assert validate_packet(packet) == []


def test_empty_packet_rejects_a_blank_ticker() -> None:
    with pytest.raises(ValueError):
        empty_packet("   ")


def test_set_section_records_success_and_failure() -> None:
    packet = empty_packet("MU", as_of=AS_OF)
    set_section(packet, "macro", {"yields": {}})
    assert packet["macro"] == {"yields": {}}
    assert packet["macro_error"] is None

    set_section(packet, "filings", None, error="SEC timed out")
    assert packet["filings"] is None
    assert packet["filings_error"] == "SEC timed out"
    assert {"source": "filings", "error": "SEC timed out"} in packet["meta"]["errors"]
    assert packet["meta"]["source_status"]["filings"] == "error"
    assert validate_packet(packet) == []


def test_meta_bookkeeping_is_deduplicated() -> None:
    packet = empty_packet("SPY", as_of=AS_OF)
    record_error(packet, "exa", "rate limited")
    record_error(packet, "exa", "rate limited")
    record_timing(packet, "macro", 1234.5678)
    record_unavailable(packet, "options", "no entitlement")
    assert len(packet["meta"]["errors"]) == 1
    assert packet["meta"]["timings_ms"]["macro"] == 1234.568
    assert packet["meta"]["unavailable"] == [
        {"source": "options", "reason": "no entitlement"}
    ]


def test_validate_packet_flags_a_section_that_is_both_set_and_failed() -> None:
    packet = empty_packet("SPY", as_of=AS_OF)
    packet["macro"] = {"a": 1}
    packet["macro_error"] = "boom"
    assert "macro is populated but macro_error is also set" in validate_packet(packet)


def test_macro_and_seasonal_skeletons_have_no_invented_numbers() -> None:
    series = empty_macro_series("DGS10", label="10Y", units="percent")
    assert series["current"] is None and series["monthly_12"] == []
    stats = empty_seasonal_stats("SPY", month=9)
    assert stats["month_label"] == "September"
    assert set(stats["this_month"]) == {"1y", "2y", "5y", "10y"}
    assert all(block["n"] == 0 for block in stats["this_month"].values())
    assert month_label(1) == "January"
    with pytest.raises(ValueError):
        month_label(13)


# --------------------------------------------------------------------------- universe


def test_delisted_symbols_are_remapped_with_a_stated_reason() -> None:
    assert normalize_symbol("fxch") == "CYB"
    assert normalize_symbol("VCHY") == "HYG"
    assert normalize_symbol("NVDA") == "NVDA"
    assert "delisted" in (remap_note("FXCH") or "")
    assert "high-yield" in (remap_note("VCHY") or "")
    assert remap_note("SPY") is None
    assert SYMBOL_REMAP["FXCH"]["note"] in (BENCHMARKS_BY_SYMBOL["CYB"].note or "")
    assert BENCHMARKS_BY_SYMBOL["HYG"].note == SYMBOL_REMAP["VCHY"]["note"]


def test_verified_coverage_limits_are_carried_into_the_universe() -> None:
    # Facts checked against the live Massive plan on 2026-09-01.
    assert "2 years" in COVERAGE_NOTES["X:BTCUSD"]
    assert "October 2023" in (BENCHMARKS_BY_SYMBOL["CYB"].note or "")
    assert "mutual funds" in (BENCHMARKS_BY_SYMBOL["FSCHX"].note or "")
    assert "mutual funds" in (BENCHMARKS_BY_SYMBOL["VMIAX"].note or "")
    assert COVERAGE_NOTES["X:BTCUSD"] in (BENCHMARKS_BY_SYMBOL["X:BTCUSD"].note or "")
    # The chemicals keyword must resolve to a symbol Massive actually serves.
    assert industry_etfs("Industrial organic chemicals") == ["XLB"]


def test_stale_series_are_flagged_not_hidden() -> None:
    index = business_days("2016-01-03", "2023-10-20")
    series = pd.Series(np.linspace(20.0, 24.0, len(index)), index=index, name="CYB")
    client = FakeClient({"CYB": ohlcv(series)})
    load = load_daily(client, "CYB", years=10, as_of=AS_OF)
    assert load.stale_days is not None and load.stale_days > 1000
    assert load.is_stale() is True
    entry = load.universe_entry(label="Chinese Renminbi", role="fx")
    assert "stale" in (entry["note"] or "")
    assert entry["n_days"] == load.n_days  # the history is still there

    fresh = load_daily(FakeClient(_frames()), "SPY", years=10, as_of=AS_OF)
    assert fresh.is_stale() is False
    assert "stale" not in (fresh.universe_entry(label="S&P 500", role="index")["note"] or "")


def test_sector_and_industry_maps() -> None:
    assert sector_etf("Technology") == "XLK"
    assert sector_etf("consumer cyclical") == "XLY"
    assert sector_etf("Specialty Industrial Machinery") == "XLI"  # substring match
    assert sector_etf("Blank Checks") is None
    assert sector_etf(None) is None
    assert industry_etfs("Semiconductors & related devices") == ["SOXX"]
    assert "XBI" in industry_etfs("Pharmaceutical preparations")
    assert industry_etfs(None, "") == []


def test_related_etfs_orders_specific_before_broad() -> None:
    etfs = related_etfs(
        {"sector": "Technology", "industry": "Semiconductors", "description": "GPUs"}
    )
    assert etfs[0] == "SOXX"
    assert "XLK" in etfs
    assert etfs[-1] == "SPY"
    assert len(etfs) <= 6


def test_universe_covers_every_asked_for_role() -> None:
    entries = resolve_universe("NVDA", profile={"sector": "Technology"})
    symbols = [entry["symbol"] for entry in entries]
    assert symbols[0] == "NVDA"
    assert entries[0]["role"] == "self"
    for expected in ("SPY", "QQQ", "XLK", "SOXX", "GLD", "X:BTCUSD", "HYG", "CYB", "TAGS"):
        assert expected in symbols, expected
    roles = {entry["role"] for entry in entries}
    assert {"index", "sector", "industry", "commodity", "fx", "credit", "crypto", "gold"} <= roles
    assert {"rates", "vol", "macro"} <= roles  # FRED members
    assert "NVDA" not in benchmark_symbols()
    assert "VIXCLS" in fred_series_ids()
    assert "DGS10" in fred_series_ids(roles=("rates",))


def test_universe_symbols_are_unique_and_lead_with_the_ticker() -> None:
    symbols = universe_symbols("SOXX", profile={"sector": "Technology"})
    assert symbols[0] == "SOXX"
    assert len(symbols) == len(set(symbols))
    assert "X:BTCUSD" in symbols
    assert "X:BTCUSD" not in universe_symbols("NVDA", include_crypto=False)


# --------------------------------------------------------------------------- cache


def test_local_cache_round_trips_and_counts_hits(tmp_path: Any) -> None:
    cache = PrismCache(base_dir=tmp_path, supabase=None)
    assert cache.get("series", "SPY", generation=AS_OF) is None
    assert cache.misses == 1

    cache.set("series", "SPY", {"provider": "massive", "payload": {"a": 1}}, generation=AS_OF)
    entry = cache.get("series", "SPY", generation=AS_OF)
    assert entry is not None and entry["provider"] == "massive"
    assert cache.hits == 1
    assert cache.status() == "hit"
    # A different as-of month is a different generation.
    assert cache.get("series", "SPY", generation=date(2026, 8, 1)) is None


def test_series_cache_round_trip_preserves_values(tmp_path: Any) -> None:
    index = business_days("2026-01-01", "2026-03-31")
    series = pd.Series(np.linspace(100.0, 120.0, len(index)), index=index, name="X:BTCUSD")
    cache = PrismCache(base_dir=tmp_path, supabase=None)
    cache.set_series("X:BTCUSD", series, meta={"provider": "massive"}, generation=AS_OF)
    loaded = cache.get_series("X:BTCUSD", generation=AS_OF)
    assert loaded is not None
    restored, meta = loaded
    pd.testing.assert_series_equal(restored, series, check_names=False, check_freq=False)
    assert meta["provider"] == "massive"
    # The colon must not escape into the filesystem path.
    assert (tmp_path / "series" / "2026-09" / "X_BTCUSD.json").exists()


def test_cache_respects_ttl_and_can_be_disabled(tmp_path: Any) -> None:
    cache = PrismCache(base_dir=tmp_path, supabase=None, ttl_days=1)
    cache.set("macro", "DGS10", {"payload": {"a": 1}}, generation=AS_OF)
    path = tmp_path / "macro" / "2026-09" / "DGS10.json"
    stale = json.loads(path.read_text())
    stale["cached_at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(stale))
    assert cache.get("macro", "DGS10", generation=AS_OF) is None

    disabled = PrismCache(base_dir=tmp_path, supabase=None, enabled=False)
    disabled.set("macro", "DGS10", {"payload": {"a": 2}}, generation=AS_OF)
    assert disabled.get("macro", "DGS10", generation=AS_OF) is None
    assert disabled.status() == "disabled"


def test_cache_helpers() -> None:
    assert as_of_month(date(2026, 9, 1)) == "2026-09"
    assert as_of_month("2026-09-01") == "2026-09"
    assert safe_key("X:BTCUSD") == "X_BTCUSD"
    assert safe_key("../etc/passwd") == "_etc_passwd"  # no path separators survive
    assert "/" not in safe_key("a/b/c")
    index = pd.to_datetime(pd.Index(["2026-01-02", "2026-01-05"]))
    series = pd.Series([1.5, 2.5], index=index)
    assert series_to_payload(series) == {
        "dates": ["2026-01-02", "2026-01-05"],
        "values": [1.5, 2.5],
    }
    pd.testing.assert_series_equal(
        payload_to_series(series_to_payload(series)), series, check_names=False
    )


class RecordingSupabaseSession:
    def __init__(self, *, rows: list[dict[str, Any]] | None = None, fail: bool = False) -> None:
        self.rows = rows or []
        self.fail = fail
        self.posts: list[dict[str, Any]] = []
        self.gets: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.gets.append({"url": url, **kwargs})
        if self.fail:
            return FakeResponse({}, status_code=500, text="boom")
        return FakeResponse(self.rows)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.posts.append({"url": url, **kwargs})
        if self.fail:
            return FakeResponse({}, status_code=500, text="boom")
        return FakeResponse([], text="")


def test_supabase_tier_reads_writes_and_degrades(tmp_path: Any) -> None:
    session = RecordingSupabaseSession(
        rows=[
            {
                "cache_key": "series:SPY:2026-09",
                "entry": {"provider": "massive", "payload": {"dates": [], "values": []}},
                "fetched_at": "2026-09-01T00:00:00+00:00",
            }
        ]
    )
    supabase = SupabaseSeriesCache(
        supabase_url="https://example.supabase.co",
        service_role_key="test-key",
        session=session,  # type: ignore[arg-type]
    )
    cache = PrismCache(base_dir=tmp_path, supabase=supabase)
    entry = cache.get("series", "SPY", generation=AS_OF)
    assert entry is not None and entry["provider"] == "massive"
    # The remote hit is written through to the local tier.
    assert (tmp_path / "series" / "2026-09" / "SPY.json").exists()

    cache.set("series", "QQQ", {"provider": "massive", "payload": {}}, generation=AS_OF)
    assert session.posts, "expected an upsert"
    body = session.posts[-1]["json"][0]
    assert body["cache_key"] == "series:QQQ:2026-09"
    assert body["as_of_month"] == "2026-09"
    assert session.posts[-1]["params"]["on_conflict"] == "cache_key"

    broken = PrismCache(
        base_dir=tmp_path / "broken",
        supabase=SupabaseSeriesCache(
            supabase_url="https://example.supabase.co",
            service_role_key="test-key",
            session=RecordingSupabaseSession(fail=True),  # type: ignore[arg-type]
        ),
    )
    assert broken.get("series", "SPY", generation=AS_OF) is None
    broken.set("series", "SPY", {"payload": {}}, generation=AS_OF)
    assert any(item["stage"] == "supabase_read" for item in broken.errors)
    assert any(item["stage"] == "supabase_write" for item in broken.errors)
    # Despite the Supabase failure the local tier still served the write.
    assert broken.get("series", "SPY", generation=AS_OF) is not None


# --------------------------------------------------------------------------- data


def _frames() -> dict[str, pd.DataFrame]:
    index = business_days("2016-01-01", "2026-08-31")
    rng = np.random.default_rng(7)
    frames: dict[str, pd.DataFrame] = {}
    for offset, symbol in enumerate(("SPY", "QQQ", "NVDA", "X:BTCUSD")):
        returns = rng.normal(0.0004, 0.011 + 0.002 * offset, len(index))
        frames[symbol] = ohlcv(geometric_series(returns, index=index, name=symbol))
    return frames


def test_load_daily_returns_series_with_provenance() -> None:
    client = FakeClient(_frames())
    load = load_daily(client, "spy", years=10, as_of=AS_OF)
    assert load.symbol == "SPY"
    assert load.provider == "massive"
    assert load.n_days == len(load.series)
    assert load.first_date is not None and load.last_date == "2026-08-31"
    assert load.coverage_years > 9.0
    provenance = load.provenance()
    assert provenance["provider"] == "massive" and provenance["symbol"] == "SPY"
    entry = load.universe_entry(label="S&P 500", role="index")
    assert entry["n_days"] == load.n_days and entry["error"] is None
    pd.testing.assert_series_equal(
        daily_closes(client, "SPY", years=10, as_of=AS_OF), load.series
    )


def test_colon_symbols_bypass_the_facades_symbol_validator() -> None:
    client = FakeClient(_frames())
    assert is_passthrough_symbol("X:BTCUSD") and not is_passthrough_symbol("SPY")
    with pytest.raises(ValueError):
        client.get_history("X:BTCUSD")
    load = load_daily(client, "X:BTCUSD", years=10, as_of=AS_OF)
    assert load.n_days > 2000
    assert client.provider.calls[0]["ticker"] == "X:BTCUSD"


def test_load_daily_shrinks_the_window_instead_of_faking_history() -> None:
    frames = _frames()
    client = FakeClient(frames, fail_windows={"SPY": 800})
    load = load_daily(client, "SPY", years=10, as_of=AS_OF)
    assert load.requested_years == 2
    windows = [(call["end"] - call["start"]).days for call in client.calls]
    assert windows == sorted(windows, reverse=True)
    assert len(windows) == 3  # 10y and 5y rejected, 2y accepted


def test_load_daily_raises_when_nothing_works() -> None:
    client = FakeClient({})
    with pytest.raises(PrismDataError) as excinfo:
        load_daily(client, "ZZZZ", years=10, as_of=AS_OF)
    assert "no daily history" in str(excinfo.value)


def test_load_daily_uses_the_cache_on_the_second_call(tmp_path: Any) -> None:
    client = FakeClient(_frames())
    cache = PrismCache(base_dir=tmp_path, supabase=None)
    first = load_daily(client, "SPY", years=10, as_of=AS_OF, cache=cache)
    calls_after_first = len(client.calls)
    second = load_daily(client, "SPY", years=10, as_of=AS_OF, cache=cache)
    assert len(client.calls) == calls_after_first
    assert second.cached is True
    pd.testing.assert_series_equal(first.series, second.series, check_freq=False)


def test_load_daily_refetches_when_cache_covers_a_shorter_window(tmp_path: Any) -> None:
    """A cached short window must not be reused to satisfy a longer request.

    A transient full-window failure can cache a truncated span; serving it for a
    later, longer request silently shrinks the sample every downstream stat uses.
    """
    client = FakeClient(_frames())
    cache = PrismCache(base_dir=tmp_path, supabase=None)

    short = load_daily(client, "SPY", years=7, as_of=AS_OF, cache=cache)
    assert short.requested_years == 7
    calls_after_short = len(client.calls)

    # A longer request than the cached span must fall through and re-fetch.
    longer = load_daily(client, "SPY", years=12, as_of=AS_OF, cache=cache)
    assert longer.cached is False
    assert len(client.calls) > calls_after_short
    assert longer.requested_years == 12
    calls_after_long = len(client.calls)

    # Now the cache covers 12y, so a shorter (or equal) request reuses it for free.
    reused = load_daily(client, "SPY", years=7, as_of=AS_OF, cache=cache)
    assert reused.cached is True
    assert len(client.calls) == calls_after_long


def test_load_universe_isolates_failures() -> None:
    client = FakeClient(_frames())
    data = load_universe(
        client, ["SPY", "QQQ", "MISSING", "X:BTCUSD"], years=10, as_of=AS_OF
    )
    assert set(data.symbols()) == {"SPY", "QQQ", "X:BTCUSD"}
    assert "MISSING" in data.errors
    entries = data.universe_entries(
        {"SPY": ("S&P 500", "index"), "MISSING": ("Nothing", "index")}
    )
    by_symbol = {entry["symbol"]: entry for entry in entries}
    assert by_symbol["MISSING"]["error"]
    assert by_symbol["SPY"]["n_days"] > 0
    assert len(data.provenance()) == 3


def test_alignment_and_window_helpers() -> None:
    index = business_days("2026-01-01", "2026-06-30")
    a = pd.Series(np.arange(len(index), dtype="float64") + 100.0, index=index, name="A")
    b = pd.Series(np.arange(len(index) - 3, dtype="float64") + 200.0, index=index[3:], name="B")
    outer = align_series({"A": a, "B": b})
    inner = align_series({"A": a, "B": b}, how="inner")
    assert list(outer.columns) == ["A", "B"]
    assert len(outer) == len(a)
    assert len(inner) == len(b)
    assert align_series({}).empty

    window = trailing_window(outer, "1m")
    assert len(window) == 21
    assert trailing_window(outer, 5).index[-1] == outer.index[-1]
    with pytest.raises(ValueError):
        trailing_window(outer, "13m")

    returns = common_window({"A": a, "B": b}, "1m")
    assert list(returns.columns) == ["A", "B"]
    assert len(returns) == 20
    assert not returns.isna().to_numpy().any()

    simple = to_returns(a)
    log = to_returns(a, log=True)
    assert len(simple) == len(a) - 1
    assert float(log.iloc[0]) == pytest.approx(math.log(101.0 / 100.0))


def test_finite_rejects_nan_inf_and_non_numbers() -> None:
    assert finite(1.5) == 1.5
    assert finite(np.float64(2.0)) == 2.0
    assert finite(float("nan")) is None
    assert finite(float("inf")) is None
    assert finite(None) is None
    assert finite(True) is None
    assert finite("abc") is None
    assert resolve_as_of("2026-09-01") == AS_OF
    assert resolve_as_of(AS_OF) == AS_OF


# --------------------------------------------------------------------------- macro


def _macro_series() -> dict[str, pd.Series]:
    index = business_days("2020-01-01", "2026-08-31")
    rng = np.random.default_rng(11)
    series: dict[str, pd.Series] = {}
    for series_id, level, scale in (
        ("DGS2", 4.2, 0.02),
        ("DGS5", 4.0, 0.02),
        ("DGS10", 4.3, 0.02),
        ("DGS20", 4.6, 0.02),
        ("T10Y2Y", 0.1, 0.01),
        ("VIXCLS", 17.0, 1.0),
        ("BAMLH0A0HYM2", 3.2, 0.05),
        ("DTWEXBGS", 120.0, 0.3),
        ("DCOILWTICO", 78.0, 1.0),
        ("DCOILBRENTEU", 82.0, 1.0),
        ("PAYEMS", 158000.0, 50.0),
        ("DEXJPUS", 148.0, 0.5),
        ("DEXUSEU", 1.08, 0.005),
        ("DEXCHUS", 7.2, 0.01),
        ("DEXSZUS", 0.88, 0.004),
        ("DEXCAUS", 1.36, 0.004),
        ("DEXUSAL", 0.66, 0.003),
    ):
        walk = level + np.cumsum(rng.normal(0.0, scale, len(index)))
        series[series_id] = pd.Series(np.abs(walk), index=index, name=series_id)
    return series


def test_fred_client_parses_observations_and_drops_gaps() -> None:
    session = FakeFredSession(_macro_series())
    client = FredClient("test-key", session=session)  # type: ignore[arg-type]
    series = client.get_series("DGS10", start=date(2024, 1, 1), end=AS_OF)
    assert not series.empty
    assert series.index.is_monotonic_increasing
    assert pd.Timestamp("1990-01-01") not in series.index  # the "." row was dropped
    params = session.requests[-1]["params"]
    assert params["series_id"] == "DGS10"
    assert params["file_type"] == "json"
    assert params["observation_start"] == "2024-01-01"


def test_fred_client_requires_a_key_and_surfaces_http_errors() -> None:
    with pytest.raises(FredError):
        FredClient("  ")
    session = FakeFredSession(_macro_series(), fail={"DGS10"})
    client = FredClient("test-key", session=session, max_retries=0)  # type: ignore[arg-type]
    with pytest.raises(FredError):
        client.get_series("DGS10")
    empty = FredClient("test-key", session=FakeFredSession({}), max_retries=0)  # type: ignore[arg-type]
    with pytest.raises(FredError):
        empty.get_series("NOPE")


def test_macro_series_uses_the_right_change_convention() -> None:
    index = pd.to_datetime(pd.Index(["2025-08-29", "2025-11-28", "2026-05-29", "2026-08-31"]))
    levels = pd.Series([4.00, 4.10, 4.20, 4.35], index=index, name="DGS10")
    payload = build_macro_series("DGS10", levels, as_of=AS_OF)
    assert payload["change_mode"] == "diff"
    assert payload["current"] == pytest.approx(4.35)
    assert payload["as_of"] == "2026-08-31"
    assert payload["change_3m"] == pytest.approx(0.15)
    # Twelve months before 2026-08-31 is 2025-08-31; the last observation on or
    # before that date is the 2025-08-29 print at 4.00.
    assert payload["change_12m"] == pytest.approx(0.35)
    assert payload["error"] is None

    prices = pd.Series([100.0, 110.0, 120.0, 132.0], index=index, name="GLD")
    priced = build_macro_series(
        "GLD", prices, provider="massive", label="Gold", units="USD", change_mode="pct"
    )
    assert priced["change_12m"] == pytest.approx(0.32)
    assert priced["provider"] == "massive"

    missing = build_macro_series("DGS10", pd.Series(dtype="float64"))
    assert missing["current"] is None and missing["error"]


def test_monthly_points_and_value_as_of() -> None:
    index = business_days("2025-06-01", "2026-08-31")
    values = pd.Series(np.linspace(1.0, 15.0, len(index)), index=index, name="X")
    points = monthly_points(values, months=12, change_mode="diff", as_of=AS_OF)
    assert len(points) == 12
    assert points[-1]["month"] == "2026-08"
    assert points[0]["change"] is not None
    assert all(point["avg"] is not None for point in points)
    assert value_as_of(values, pd.Timestamp("2026-08-31")) == pytest.approx(15.0)
    assert value_as_of(values, pd.Timestamp("2020-01-01")) is None
    assert monthly_points(pd.Series(dtype="float64")) == []


def test_curve_shape_labels() -> None:
    def yields(two: float, five: float, ten: float, twenty: float) -> dict[str, Any]:
        return {
            "DGS2": empty_macro_series("DGS2") | {"current": two},
            "DGS5": empty_macro_series("DGS5") | {"current": five},
            "DGS10": empty_macro_series("DGS10") | {"current": ten},
            "DGS20": empty_macro_series("DGS20") | {"current": twenty},
        }

    assert curve_shape(yields(4.5, 4.3, 4.1, 4.4))["label"] == "inverted"
    assert curve_shape(yields(4.0, 4.05, 4.1, 4.4))["label"] == "flat"
    assert curve_shape(yields(3.5, 4.0, 4.4, 4.8))["label"] == "normal"
    assert curve_shape(yields(2.0, 3.0, 4.0, 4.8))["label"] == "steep"
    assert curve_shape(yields(3.5, 4.0, 4.4, 4.8))["2s10s"] == pytest.approx(0.9)
    assert curve_shape({})["label"] == "unknown"


def test_build_macro_section_without_fred_is_honest_not_empty() -> None:
    section = build_macro_section(None, as_of=AS_OF)
    assert section["yields"]["DGS10"]["current"] is None
    assert "FRED_API_KEY" in section["yields"]["DGS10"]["error"]
    assert section["gold"]["error"]
    assert section["curve_shape"]["label"] == "unknown"
    assert any(item["source"] == "fred" for item in section["unavailable"])
    assert set(section["fx"]) == {"JPY", "EUR", "CNY", "CHF", "CAD", "AUD"}


def test_build_macro_section_with_fakes(tmp_path: Any) -> None:
    fred = FredClient("test-key", session=FakeFredSession(_macro_series()))  # type: ignore[arg-type]
    client = FakeClient(_frames() | {"GLD": ohlcv(_gld_series())})
    cache = PrismCache(base_dir=tmp_path, supabase=None)
    section = build_macro_section(
        fred, market_client=client, as_of=AS_OF, cache=cache, max_workers=2
    )
    assert section["yields"]["DGS10"]["current"] is not None
    assert section["yields"]["DGS10"]["error"] is None
    assert len(section["vix"]["monthly"]) == 12
    assert section["vix"]["monthly"][0].keys() == {"month", "avg", "change"}
    assert section["curve_shape"]["label"] in {"inverted", "flat", "normal", "steep"}
    assert section["gold"]["provider"] == "massive"
    assert section["gold"]["current"] is not None
    assert section["btc"]["series_id"] == "X:BTCUSD"
    assert section["nfp"]["units"] == "thousands of persons"
    assert section["fx"]["JPY"]["series_id"] == "DEXJPUS"

    # Second build is served entirely from cache: no new FRED requests.
    session = FakeFredSession({})
    cached_fred = FredClient("test-key", session=session)  # type: ignore[arg-type]
    again = build_macro_section(
        cached_fred, market_client=client, as_of=AS_OF, cache=cache, max_workers=2
    )
    assert session.requests == []
    assert again["yields"]["DGS10"]["current"] == section["yields"]["DGS10"]["current"]


def _gld_series() -> pd.Series:
    index = business_days("2016-01-01", "2026-08-31")
    rng = np.random.default_rng(3)
    return geometric_series(
        rng.normal(0.0003, 0.008, len(index)), index=index, base=120.0, name="GLD"
    )


# --------------------------------------------------------------------------- seasonality


def test_monthly_returns_ignore_the_open_month() -> None:
    series = step_series(
        start="2024-01-01",
        end="2026-09-15",
        monthly_factor=lambda _year, _month: 1.01,
    )
    closes = month_end_closes(series, as_of=date(2026, 9, 15))
    assert str(closes.index[-1]) == "2026-08"
    returns = monthly_returns(series, as_of=date(2026, 9, 15))
    assert float(returns.iloc[-1]) == pytest.approx(0.01)


def test_this_month_stats_are_exact_on_a_known_pattern() -> None:
    def factor(_year: int, month: int) -> float:
        if month == 9:
            return 1.03
        return 1.001

    series = step_series(monthly_factor=factor, name="SYN")
    stats = this_month_stats(series, month=9, as_of=AS_OF)
    assert stats["10y"]["n"] == 10
    assert stats["10y"]["mean"] == pytest.approx(0.03)
    assert stats["10y"]["median"] == pytest.approx(0.03)
    assert stats["10y"]["hit_rate"] == pytest.approx(1.0)
    assert stats["1y"]["n"] == 1
    assert stats["1y"]["values"][0]["year"] == 2025.0
    assert {int(row["year"]) for row in stats["2y"]["values"]} == {2024, 2025}


def test_seasonal_trend_direction_follows_recent_strength() -> None:
    def accelerating(year: int, month: int) -> float:
        if month == 9:
            return 1.0 + 0.005 * (year - 2013)
        return 1.0

    def decelerating(year: int, month: int) -> float:
        if month == 9:
            return 1.0 + 0.005 * (2027 - year)
        return 1.0

    up = seasonal_trend(
        this_month_stats(step_series(monthly_factor=accelerating), month=9, as_of=AS_OF)
    )
    down = seasonal_trend(
        this_month_stats(step_series(monthly_factor=decelerating), month=9, as_of=AS_OF)
    )
    flat = seasonal_trend(
        this_month_stats(
            step_series(monthly_factor=lambda _year, month: 1.02 if month == 9 else 1.0),
            month=9,
            as_of=AS_OF,
        )
    )
    assert up["direction"] == "accelerating" and up["slope"] is not None and up["slope"] < 0
    assert down["direction"] == "decelerating" and down["slope"] > 0
    assert flat["direction"] == "flat"
    assert seasonal_trend({})["direction"] == "flat"


def test_forward_returns_compound_the_known_months() -> None:
    factors = {9: 1.03, 10: 1.02, 11: 1.01}

    def factor(_year: int, month: int) -> float:
        return factors.get(month, 1.0)

    stats = seasonal_stats(step_series(monthly_factor=factor), month=9, symbol="SYN", as_of=AS_OF)
    forward = stats["forward"]
    assert forward["1m"]["mean"] == pytest.approx(0.03)
    assert forward["2m"]["mean"] == pytest.approx(1.03 * 1.02 - 1)
    assert forward["3m"]["mean"] == pytest.approx(1.03 * 1.02 * 1.01 - 1)
    assert forward["1m"]["p10"] == pytest.approx(0.03)
    assert forward["1m"]["hit_rate"] == pytest.approx(1.0)
    # Forward 1m must agree with the this-month block by construction.
    assert stats["this_month"]["10y"]["mean"] == pytest.approx(forward["1m"]["mean"])
    # 18m needs months that do not exist yet for the most recent years.
    assert forward["18m"]["n"] < forward["1m"]["n"]


def test_seasonal_stats_reports_short_history_instead_of_guessing() -> None:
    short = step_series(start="2026-06-01", end="2026-08-31", monthly_factor=lambda _y, _m: 1.01)
    stats = seasonal_stats(short, month=9, symbol="NEW", as_of=AS_OF)
    assert stats["error"] is not None
    assert stats["this_month"]["10y"]["n"] == 0
    assert seasonal_stats(pd.Series(dtype="float64"), month=9)["error"] == "no price history"


def test_build_seasonality_section_shape() -> None:
    ticker = step_series(monthly_factor=lambda _y, m: 1.03 if m == 9 else 1.001, name="NVDA")
    spy = step_series(monthly_factor=lambda _y, m: 1.005 if m == 9 else 1.002, name="SPY")
    section = build_seasonality_section(
        "NVDA", ticker, {"SPY": spy, "NVDA": ticker}, as_of=AS_OF
    )
    assert section["month"] == 9 and section["month_label"] == "September"
    assert section["ticker"]["symbol"] == "NVDA"
    assert set(section["benchmarks"]) == {"SPY"}
    assert section["ticker"]["this_month"]["10y"]["mean"] > (
        section["benchmarks"]["SPY"]["this_month"]["10y"]["mean"]
    )


# --------------------------------------------------------------------------- relational


def _relational_frame() -> dict[str, pd.Series]:
    index = business_days("2016-01-01", "2026-08-31")
    rng = np.random.default_rng(42)
    market = rng.normal(0.0004, 0.010, len(index))
    idiosyncratic = rng.normal(0.0, 0.004, len(index))
    unrelated = rng.normal(0.0002, 0.009, len(index))
    return {
        "SPY": geometric_series(market, index=index, name="SPY"),
        "NVDA": geometric_series(1.5 * market + idiosyncratic, index=index, name="NVDA"),
        "QQQ": geometric_series(1.1 * market + 0.3 * idiosyncratic, index=index, name="QQQ"),
        "GLD": geometric_series(unrelated, index=index, name="GLD"),
    }


def test_ols_and_rolling_beta_recover_a_known_loading() -> None:
    series = _relational_frame()
    frame = align_series(series)
    returns = frame.pct_change().iloc[1:]
    beta, alpha = ols_beta(returns["NVDA"], returns["SPY"])
    assert beta == pytest.approx(1.5, abs=0.05)
    assert alpha is not None and abs(alpha) < 0.001
    unrelated_beta, _ = ols_beta(returns["GLD"], returns["SPY"])
    assert abs(unrelated_beta or 0.0) < 0.2

    rolling = rolling_beta(returns["NVDA"], returns["SPY"], window=63)
    assert len(rolling) > 2000
    assert float(rolling.mean()) == pytest.approx(1.5, abs=0.1)
    assert rolling_beta(returns["NVDA"].iloc[:10], returns["SPY"].iloc[:10]).empty
    assert ols_beta(returns["NVDA"].iloc[:2], returns["SPY"].iloc[:2]) == (None, None)


def test_gauge_fix_produces_a_zero_mean_unit_variance_frame() -> None:
    frame = align_series(_relational_frame())
    returns = frame.pct_change().iloc[1:]
    fixed = gauge_fix(returns, reference="SPY")
    assert "SPY" not in fixed.columns
    assert set(fixed.columns) == {"NVDA", "QQQ", "GLD"}
    assert float(fixed["NVDA"].mean()) == pytest.approx(0.0, abs=1e-9)
    assert float(fixed["NVDA"].std(ddof=1)) == pytest.approx(1.0, abs=1e-9)
    # The gauge-fixed frame removes the shared market factor, so NVDA and QQQ
    # correlate through their shared idiosyncratic term, not through SPY.
    assert fixed["NVDA"].corr(fixed["GLD"]) < fixed["NVDA"].corr(fixed["QQQ"])
    assert gauge_fix(returns, reference="MISSING").empty
    constant = pd.DataFrame({"A": [0.01] * 50, "SPY": [0.01] * 50})
    assert gauge_fix(constant, reference="SPY").empty


def test_kinematics_reads_a_constant_drift_and_a_ramp() -> None:
    index = business_days("2020-01-01", "2026-08-31")
    steady = geometric_series(np.full(len(index), 0.001), index=index, name="STEADY")
    result = kinematics(steady)
    assert result["velocity"] == pytest.approx(0.001, abs=5e-5)
    assert abs(result["acceleration"] or 1.0) < 1e-5
    assert abs(result["jerk"] or 1.0) < 1e-5
    assert result["window_days"] == 21

    ramp_returns = np.linspace(0.0, 0.002, len(index))
    ramp = geometric_series(ramp_returns, index=index, name="RAMP")
    accelerating = kinematics(ramp)
    assert accelerating["acceleration"] is not None and accelerating["acceleration"] > 0
    assert kinematics(steady.iloc[:10])["velocity"] is None


def test_similarity_and_covariance_matrices() -> None:
    frame = align_series(_relational_frame())
    returns = frame.pct_change().iloc[1:].dropna()
    cosine = cosine_similarity_matrix(returns)
    assert cosine["symbols"] == list(returns.columns)
    diagonal = [cosine["matrix"][i][i] for i in range(len(cosine["symbols"]))]
    assert all(value == pytest.approx(1.0) for value in diagonal)
    index_spy = cosine["symbols"].index("SPY")
    index_nvda = cosine["symbols"].index("NVDA")
    index_gld = cosine["symbols"].index("GLD")
    assert cosine["matrix"][index_spy][index_nvda] > cosine["matrix"][index_spy][index_gld]

    covariance = covariance_matrix(returns)
    assert covariance["annualized"] is True
    size = len(covariance["symbols"])
    for row in range(size):
        for column in range(size):
            assert covariance["matrix"][row][column] == pytest.approx(
                covariance["matrix"][column][row]
            )
    assert cosine_similarity_matrix(returns[["SPY"]])["matrix"] == []


def test_impact_weights_sum_to_one_and_rank_by_explained_variance() -> None:
    frame = align_series(_relational_frame())
    returns = frame.pct_change().iloc[1:].dropna()
    weights = impact_weights("NVDA", returns)
    assert set(weights) == {"SPY", "QQQ", "GLD"}
    assert sum(entry["weight"] or 0.0 for entry in weights.values()) == pytest.approx(1.0)
    assert weights["SPY"]["explained_variance_share"] > weights["GLD"]["explained_variance_share"]
    assert impact_weights("MISSING", returns) == {}


def test_trend_label_thresholds() -> None:
    rising = pd.Series(np.linspace(1.0, 2.0, 100))
    falling = pd.Series(np.linspace(2.0, 1.0, 100))
    flat = pd.Series(np.full(100, 1.5))
    assert trend_label(rising) == "rising"
    assert trend_label(falling) == "falling"
    assert trend_label(flat) == "flat"
    assert trend_label(pd.Series([1.0, 2.0])) == "flat"


def test_relative_moving_average_reports_a_signal() -> None:
    frame = align_series(_relational_frame())
    result = relative_moving_average("NVDA", frame, window="1y")
    assert result["signal"] in {"leading", "lagging", "in_line"}
    assert result["value"] is not None
    assert sum(result["components"].values()) == pytest.approx(1.0)
    assert relative_moving_average("MISSING", frame)["signal"] == "unknown"


def test_build_relational_section_is_complete_and_gauge_fixed() -> None:
    series = _relational_frame()
    section = build_relational_section("NVDA", series, reference="SPY", as_of=AS_OF)
    assert section["reference_frame"] == "excess_over_SPY_zscored"
    assert section["reference_symbol"] == "SPY"
    assert section["beta"]["SPY"]["1y"] == pytest.approx(1.5, abs=0.2)
    assert section["beta"]["SPY"]["rolling_trend"] in {"rising", "falling", "flat"}
    assert section["correlation"]["SPY"]["1y"] > section["correlation"]["GLD"]["1y"]
    assert set(section["correlation_gauge_fixed"]) == {"SPY", "QQQ", "GLD"}
    assert section["kinematics"]["NVDA"]["velocity"] is not None
    assert section["cosine_similarity"]["symbols"]
    assert section["impact_weights"]["SPY"]["weight"] is not None
    assert section["windows"] == ["3m", "6m", "1y", "2y", "5y", "10y"]
    assert json.loads(json.dumps(section)) == section  # JSON-serialisable


def test_build_relational_section_without_the_reference_says_so() -> None:
    series = {key: value for key, value in _relational_frame().items() if key != "SPY"}
    section = build_relational_section("NVDA", series, reference="SPY", as_of=AS_OF)
    assert section["reference_frame"] == "raw_returns"
    assert section["reference_symbol"] is None
    assert "unavailable" in section["reference_note"]
    assert section["correlation_gauge_fixed"] == {}
    with pytest.raises(ValueError):
        build_relational_section("ZZZZ", series)


# --------------------------------------------------------------------------- live smoke

live = pytest.mark.skipif(
    os.getenv("PRISM_LIVE") != "1",
    reason="set PRISM_LIVE=1 with MASSIVE_API_KEY and FRED_API_KEY for live smoke tests",
)


@live
def test_live_massive_daily_history() -> None:
    from app.prism.data import build_prism_client

    client = build_prism_client()
    spy = load_daily(client, "SPY", years=10)
    assert spy.n_days > 2000
    assert spy.coverage_years > 9.0
    assert spy.provider == "massive"
    assert float(spy.series.iloc[-1]) > 0

    # Crypto is entitled to ~2 years of daily bars on this plan (verified
    # 2026-09-01), which the loader reports rather than padding.
    btc = load_daily(client, "X:BTCUSD", years=5)
    assert btc.n_days > 600
    assert btc.coverage_years > 1.5
    assert float(btc.series.iloc[-1]) > 0

    universe = load_universe(client, ["SPY", "QQQ", "GLD", "HYG", "CYB", "TAGS"], years=10)
    assert set(universe.symbols()) >= {"SPY", "QQQ", "GLD", "HYG", "TAGS"}
    assert not universe.errors, universe.errors
    # CYB stopped trading in 2023; the history loads but must be flagged stale.
    assert universe.loads["CYB"].is_stale() is True
    assert universe.loads["SPY"].is_stale() is False

    # Mutual funds are not served by this plan; the failure must be an honest
    # error entry, never a silently substituted series.
    funds = load_universe(client, ["FSCHX", "VMIAX"], years=10)
    assert set(funds.errors) == {"FSCHX", "VMIAX"}
    assert funds.series == {}


@live
def test_live_fred_macro_section() -> None:
    from app.prism.data import build_prism_client

    fred = FredClient.from_env()
    assert fred is not None, "FRED_API_KEY is required for the live test"
    ten_year = fred.get_series("DGS10", start=date(2024, 1, 1))
    assert not ten_year.empty
    assert 0.0 < float(ten_year.iloc[-1]) < 20.0

    section = build_macro_section(fred, market_client=build_prism_client())
    assert section["yields"]["DGS10"]["current"] is not None
    assert section["vix"]["current"] is not None
    assert section["gold"]["current"] is not None
    assert section["curve_shape"]["label"] != "unknown"
    assert section["fx"]["JPY"]["current"] is not None
    assert json.loads(json.dumps(section)) == section


@live
def test_live_seasonality_and_relational_for_spy() -> None:
    from app.prism.data import build_prism_client

    client = build_prism_client()
    data = load_universe(client, ["SPY", "QQQ", "XLK", "GLD"], years=10)
    assert "SPY" in data.series
    seasonality = build_seasonality_section("SPY", data.series["SPY"], data.series)
    assert seasonality["ticker"]["this_month"]["10y"]["n"] >= 9
    relational = build_relational_section("QQQ", data.series, reference="SPY")
    assert relational["beta"]["SPY"]["1y"] is not None
    assert 0.5 < float(relational["beta"]["SPY"]["1y"]) < 2.0
