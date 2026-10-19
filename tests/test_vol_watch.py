from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app import vol_watch as vw


def _frame(
    spot: float, strikes: list[float], vol: float, t: float, is_call: bool, rel: float = 0.05
):
    """A chain priced from one vol so the solver has a known answer."""
    rows = []
    for k in strikes:
        mid = vw.bs_price(spot, k, t, vol, is_call)
        half = mid * rel / 2
        rows.append({"strike": k, "bid": mid - half, "ask": mid + half})
    return pd.DataFrame(rows)


def test_implied_vol_round_trips_black_scholes() -> None:
    price = vw.bs_price(100.0, 100.0, 30 / 365, 0.25, True)
    iv = vw.implied_vol(price, 100.0, 100.0, 30 / 365, True)
    assert iv is not None and iv == pytest.approx(0.25, abs=1e-4)
    assert vw.implied_vol(0.0, 100.0, 100.0, 0.1, True) is None  # worthless quote
    assert vw.implied_vol(0.5, 100.0, 120.0, 0.1, False) is None  # below intrinsic


def test_quote_filter_drops_wide_zero_and_crossed_markets() -> None:
    t = 30 / 365
    frame = _frame(100.0, [95, 100, 105], 0.30, t, True)
    frame.loc[0, ["bid", "ask"]] = [0.0, 1.0]  # no bid
    frame.loc[2, "ask"] = frame.loc[2, "bid"] * 1.5  # (ask-bid)/mid = 0.4 > 0.25
    quotes = vw.passing_quotes(frame, 100.0, t, True, 0.25)
    assert [q.strike for q in quotes] == [100.0]
    assert quotes[0].iv == pytest.approx(0.30, abs=1e-3)
    assert 0.45 < quotes[0].delta < 0.6


def test_nearest_delta_respects_tolerance() -> None:
    t = 30 / 365
    quotes = vw.passing_quotes(
        _frame(100.0, [80, 90, 100, 110, 120], 0.30, t, True), 100.0, t, True, 0.25
    )
    atm = vw.nearest_delta(quotes, 0.5)
    assert atm is not None and atm.strike == 100.0
    assert vw.nearest_delta([q for q in quotes if q.strike == 80.0], 0.25, tolerance=0.08) is None


def test_parity_forward_recovers_the_forward_with_dividends() -> None:
    t = 30 / 365
    fwd = 98.0  # spot 100 with a dividend-like drag
    disc = fwd * vw.math.exp(-vw.RISK_FREE * t)
    calls = _frame(disc, [90, 95, 100, 105], 0.30, t, True)
    puts = _frame(disc, [90, 95, 100, 105], 0.30, t, False)
    got = vw.parity_forward(calls, puts, t, 0.25)
    assert got is not None and got == pytest.approx(fwd, abs=0.05)
    assert vw.parity_forward(calls, pd.DataFrame(), t, 0.25) is None


def test_realized_vol_matches_manual_computation() -> None:
    closes = pd.Series([100.0, 101.0, 99.0, 102.0, 101.0] * 6)
    rv = vw.realized_vol(closes, 20)
    assert rv is not None and 10 < rv < 60
    assert vw.realized_vol(closes.head(5), 20) is None


def test_interpolate_30d_in_variance_time() -> None:
    assert vw.interpolate_30d([(20, 20.0), (40, 20.0)]) == pytest.approx(20.0)
    mid = vw.interpolate_30d([(20, 10.0), (40, 30.0)])
    assert mid is not None and 10.0 < mid < 30.0
    assert vw.interpolate_30d([(45, 25.0)]) == 25.0
    assert vw.interpolate_30d([]) is None


def test_iv_rank() -> None:
    assert vw.iv_rank(30.0, [20.0, 40.0]) == 50.0
    assert vw.iv_rank(50.0, [20.0, 40.0]) == 100.0
    assert vw.iv_rank(30.0, [30.0]) is None


def test_crossings_fire_on_band_change_and_dedupe_per_30_min(tmp_path: Path) -> None:
    cfg = vw.Config(watchlist=["TSLA"], state_path=tmp_path / "s.json")
    state = vw.State()
    t0 = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)
    rich = {"iv_rank": 70.0, "spread": 10.0, "skew": 2.0}
    first = vw.crossings("TSLA", rich, state, cfg, t0)
    assert {a["signal"] for a in first} == {"iv_rank", "spread"}
    assert vw.crossings("TSLA", rich, state, cfg, t0 + timedelta(minutes=5)) == []
    # flips band 10 minutes later: band is recorded but the push is deduped
    cheap = {"iv_rank": 10.0, "spread": -5.0, "skew": 15.0}
    again = vw.crossings("TSLA", cheap, state, cfg, t0 + timedelta(minutes=10))
    assert {(a["signal"], a["band"]) for a in again} == {("skew", "high")}
    assert state.signals["TSLA:iv_rank"] == "low"
    # flips back after the window: fires again
    later = vw.crossings("TSLA", rich, state, cfg, t0 + timedelta(minutes=45))
    assert {a["signal"] for a in later} == {"iv_rank", "spread"}
    # unknown skew (no 25-delta quote) and rank n/a never fire
    assert vw.crossings("MU", {"iv_rank": None, "spread": 1.0, "skew": None}, state, cfg, t0) == []


def _bars(values: list[float], start: datetime) -> pd.Series:
    idx = pd.DatetimeIndex([start + timedelta(minutes=5 * i) for i in range(len(values))])
    return pd.Series(values, index=idx)


def test_fast_drop_detects_hour_drop_with_vix_jump() -> None:
    start = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)
    spy = _bars([600, 601, 600.5, 598, 596, 595.5], start)  # -0.9% off the high
    vix = _bars([18, 18, 18.5, 19.5, 20, 20.2], start)  # +12% off the low
    hit = vw.fast_drop(spy, vix, 60, 0.75, 8.0)
    assert hit is not None and hit["drop_pct"] < -0.75 and hit["vix_jump_pct"] > 8
    calm_vix = _bars([18, 18, 18.1, 18.2, 18.3, 18.4], start)
    assert vw.fast_drop(spy, calm_vix, 60, 0.75, 8.0) is None
    old_drop = _bars([600, 594] + [594] * 14, start)  # drop fell outside the window
    assert vw.fast_drop(old_drop, vix, 30, 0.75, 8.0) is None


def test_session_move() -> None:
    start = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)
    m = vw.session_move(_bars([100, 101, 99.5, 100.5], start))
    assert m == {"move_pct": 0.5, "range_pct": 1.5}
    assert vw.session_move(pd.Series(dtype=float)) is None


def test_state_round_trip_and_sample_pruning(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = vw.State()
    state.record_iv_sample("MU", date(2024, 1, 1), 40.0)
    state.record_iv_sample("MU", date(2026, 10, 16), 55.123456)  # prunes the stale sample
    state.signals["MU:iv_rank"] = "high"
    state.last_alert["MU:iv_rank"] = "2026-10-16T14:00:00+00:00"
    state.save(path)
    loaded = vw.State.load(path)
    assert loaded.signals == {"MU:iv_rank": "high"}
    assert loaded.last_alert == {"MU:iv_rank": "2026-10-16T14:00:00+00:00"}
    assert loaded.iv_samples == {"MU": {"2026-10-16": 55.1235}}
    assert vw.State.load(tmp_path / "missing.json").signals == {}
    # samples stored under an older iv method are dropped, signals are kept
    old = tmp_path / "old.json"
    old.write_text(
        '{"iv_method": 1, "signals": {"MU:spread": "high"},'
        ' "iv_samples": {"MU": {"2026-10-16": 67.0}}}'
    )
    legacy = vw.State.load(old)
    assert legacy.iv_samples == {} and legacy.signals == {"MU:spread": "high"}


def test_market_hours_in_eastern_time() -> None:
    assert vw.is_market_open(datetime(2026, 10, 16, 14, 0, tzinfo=UTC))  # fri 10:00 et
    assert not vw.is_market_open(datetime(2026, 10, 16, 20, 30, tzinfo=UTC))  # 16:30 et
    assert not vw.is_market_open(datetime(2026, 10, 17, 15, 0, tzinfo=UTC))  # saturday


class _FakeData:
    def __init__(
        self, drop: bool, iv: dict[str, float | None] | None = None, vix: float = 18.0
    ) -> None:
        self.drop = drop
        self.iv = iv or {}
        self.vix = vix

    def daily_closes(self, _ticker: str, _period: str = "2y") -> pd.Series:
        return pd.Series([100 + (i % 7) * 0.8 for i in range(300)])

    def intraday_closes(self, ticker: str) -> pd.Series:
        start = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)
        if ticker == "VIX":
            return _bars([18, 18, 19, 20, 20.5, 20.6] if self.drop else [18] * 6, start)
        return _bars([600, 601, 599, 597, 595, 595] if self.drop else [600] * 6, start)

    def vix_level(self) -> float | None:
        return self.vix

    def chain_metrics(self, ticker: str, _spot: float, _today: date) -> dict:
        iv = self.iv.get(ticker, 45.0)
        if iv is None:
            raise RuntimeError(f"{ticker}: no ATM quote passed the filter")
        return {"iv30": iv, "skew": 3.0, "expiries": ["2026-11-20"], "samples": [(30.0, iv)]}

    def zero_dte(self, _ticker: str, _spot: float, _today: date) -> dict | None:
        return {
            "straddle_mid": 5.0,
            "implied_move_pct": 0.65,
            "realized_move_pct": -0.2,
            "realized_range_pct": 0.5,
        }


def test_tick_end_to_end_pushes_once_and_respects_cooldown(tmp_path: Path) -> None:
    cfg = vw.Config(watchlist=["XSP", "MU"], state_path=tmp_path / "s.json")
    state = vw.State()
    notifier = vw.Notifier(cfg)
    now = datetime(2026, 10, 16, 14, 30, tzinfo=UTC)
    data = _FakeData(drop=True, iv={"XSP": 19.0}, vix=18.5)  # XSP inside the 2pt VIX gate
    result = vw.tick(cfg, state, data, notifier, now=now)  # type: ignore[arg-type]
    assert result["chains_ran"] is True
    assert [d["proxy"] for d in result["drops"]] == ["SPY"]
    fired = {(a["ticker"], a["signal"]) for a in result["signals"]}
    assert fired == {("MU", "spread"), ("XSP", "spread")}  # rank n/a never fires
    xsp_push = next(s for s in notifier.sent if s["title"].startswith("XSP"))
    assert (
        "rank n/a" in xsp_push["body"] and "0dte straddle 5.00 = 0.65% implied" in xsp_push["body"]
    )
    mu_push = next(s for s in notifier.sent if s["title"].startswith("MU"))
    assert "0dte" not in mu_push["body"]
    pushed = len(notifier.sent)
    again = vw.tick(cfg, state, data, notifier, now=now + timedelta(minutes=5))  # type: ignore[arg-type]
    assert again["drops"] == [] and again["chains_ran"] is False
    assert len(notifier.sent) == pushed
    assert state.iv_samples["XSP"] == {"2026-10-16": 19.0}


def test_sanity_gates_skip_ticker_and_send_nothing(tmp_path: Path) -> None:
    cfg = vw.Config(watchlist=["XSP", "SOYB", "CANE"], state_path=tmp_path / "s.json")
    state = vw.State()
    notifier = vw.Notifier(cfg)
    now = datetime(2026, 10, 16, 14, 30, tzinfo=UTC)
    data = _FakeData(drop=False, iv={"XSP": 25.0, "SOYB": 3.0, "CANE": None}, vix=18.0)
    result = vw.tick(cfg, state, data, notifier, now=now)  # type: ignore[arg-type]
    assert result["signals"] == [] and notifier.sent == []
    assert state.iv_samples == {} and state.last_metrics == {}


def test_rank_requires_full_sample_window(tmp_path: Path) -> None:
    cfg = vw.Config(watchlist=["MU"], state_path=tmp_path / "s.json", rank_samples=3)
    state = vw.State()
    for i, iv in enumerate([30.0, 40.0, 50.0]):
        state.record_iv_sample("MU", date(2026, 10, 1) + timedelta(days=i), iv)
    data = _FakeData(drop=False, iv={"MU": 45.0})
    m = vw.compute_metrics("MU", data, state, cfg, date(2026, 10, 16), 18.0)  # type: ignore[arg-type]
    assert m["iv_rank"] == 75.0 and m["iv_rank_samples"] == 3
    state2 = vw.State()
    state2.record_iv_sample("MU", date(2026, 10, 1), 30.0)
    m2 = vw.compute_metrics("MU", data, state2, cfg, date(2026, 10, 16), 18.0)  # type: ignore[arg-type]
    assert m2["iv_rank"] is None and m2["iv_rank_samples"] == 1
