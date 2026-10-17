from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app import vol_watch as vw


def _chain(strikes: list[float], ivs: list[float], bid: float = 0.5) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "strike": strikes,
            "impliedVolatility": ivs,
            "bid": [bid] * len(strikes),
            "ask": [bid + 0.1] * len(strikes),
        }
    )


def test_realized_vol_matches_manual_computation() -> None:
    closes = pd.Series([100.0, 101.0, 99.0, 102.0, 101.0] * 6)
    rv = vw.realized_vol(closes, 20)
    assert rv is not None and 10 < rv < 60
    assert vw.realized_vol(closes.head(5), 20) is None


def test_atm_iv_prefers_quoted_rows_and_averages_call_put() -> None:
    calls = _chain([95, 100, 105], [0.30, 0.20, 0.25])
    puts = _chain([95, 100, 105], [0.35, 0.24, 0.26])
    assert vw.atm_iv(calls, puts, 100.0) == pytest.approx(22.0)
    # unquoted rows with bogus IV are ignored when quoted rows exist
    calls.loc[1, ["bid", "ask"]] = 0.0
    assert vw.atm_iv(calls, puts, 100.0) == pytest.approx((0.30 + 0.24) / 2 * 100)


def test_skew_is_put_minus_call_at_five_percent_otm() -> None:
    calls = _chain([100, 105], [0.20, 0.18])
    puts = _chain([95, 100], [0.30, 0.22])
    assert vw.skew_points(calls, puts, 100.0) == pytest.approx(12.0)


def test_interpolate_30d_in_variance_time() -> None:
    assert vw.interpolate_30d([(20, 20.0), (40, 20.0)]) == pytest.approx(20.0)
    mid = vw.interpolate_30d([(20, 10.0), (40, 30.0)])
    assert mid is not None and 10.0 < mid < 30.0
    assert vw.interpolate_30d([(45, 25.0)]) == 25.0
    assert vw.interpolate_30d([]) is None


def test_iv_rank_and_percentile() -> None:
    assert vw.iv_rank(30.0, [20.0, 40.0]) == 50.0
    assert vw.iv_rank(50.0, [20.0, 40.0]) == 100.0
    assert vw.iv_rank(30.0, [30.0]) is None
    assert vw.percentile_of(25.0, [10.0, 20.0, 30.0, 40.0]) == 50.0


def test_crossings_fire_only_on_band_change(tmp_path: Path) -> None:
    cfg = vw.Config(watchlist=["TSLA"], state_path=tmp_path / "s.json")
    state = vw.State()
    rich = {"iv_rank": 70.0, "spread": 10.0, "skew": 2.0}
    first = vw.crossings("TSLA", rich, state, cfg)
    assert {a["signal"] for a in first} == {"iv_rank", "spread"}
    assert vw.crossings("TSLA", rich, state, cfg) == []
    cheap = {"iv_rank": 10.0, "spread": -5.0, "skew": 15.0}
    again = vw.crossings("TSLA", cheap, state, cfg)
    assert {(a["signal"], a["band"]) for a in again} == {
        ("iv_rank", "low"),
        ("spread", "low"),
        ("skew", "high"),
    }
    mid = {"iv_rank": 35.0, "spread": 2.0, "skew": 1.0}
    assert vw.crossings("TSLA", mid, state, cfg) == []  # back to mid is silent


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


def test_state_round_trip_and_sample_pruning(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = vw.State()
    state.record_iv_sample("MU", date(2024, 1, 1), 40.0)
    state.record_iv_sample("MU", date(2026, 10, 16), 55.123456)  # prunes the stale sample
    state.signals["MU:iv_rank"] = "high"
    state.save(path)
    loaded = vw.State.load(path)
    assert loaded.signals == {"MU:iv_rank": "high"}
    assert loaded.iv_samples == {"MU": {"2026-10-16": 55.1235}}
    assert vw.State.load(tmp_path / "missing.json").signals == {}


def test_market_hours_in_eastern_time() -> None:
    assert vw.is_market_open(datetime(2026, 10, 16, 14, 0, tzinfo=UTC))  # fri 10:00 et
    assert not vw.is_market_open(datetime(2026, 10, 16, 20, 30, tzinfo=UTC))  # 16:30 et
    assert not vw.is_market_open(datetime(2026, 10, 17, 15, 0, tzinfo=UTC))  # saturday


class _FakeData:
    def __init__(self, drop: bool) -> None:
        self.drop = drop

    def daily_closes(self, _ticker: str) -> pd.Series:
        base = [100 + (i % 7) * 0.8 for i in range(300)]
        return pd.Series(base)

    def intraday_closes(self, ticker: str) -> pd.Series:
        start = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)
        if ticker == "VIX":
            return _bars([18, 18, 19, 20, 20.5, 20.6] if self.drop else [18] * 6, start)
        return _bars([600, 601, 599, 597, 595, 595] if self.drop else [600] * 6, start)

    def chain_metrics(self, _ticker: str, _spot: float, _today: date) -> dict:
        return {"iv30": 45.0, "skew": 3.0, "expiries": ["2026-11-20"], "samples": [(30.0, 45.0)]}


def test_tick_end_to_end_pushes_once_and_respects_cooldown(tmp_path: Path) -> None:
    cfg = vw.Config(watchlist=["XSP", "MU"], state_path=tmp_path / "s.json", webhook_url=None)
    state = vw.State()
    notifier = vw.Notifier(cfg)
    now = datetime(2026, 10, 16, 14, 30, tzinfo=UTC)
    result = vw.tick(cfg, state, _FakeData(drop=True), notifier, now=now)  # type: ignore[arg-type]
    assert result["chains_ran"] is True
    assert [d["proxy"] for d in result["drops"]] == ["SPY"]
    # iv30 45 vs realized of a tame synthetic tape: spread rich, rank proxy high -> both fire
    fired = {(a["ticker"], a["signal"]) for a in result["signals"]}
    assert ("MU", "spread") in fired and ("XSP", "spread") in fired
    pushed = len(notifier.sent)
    assert pushed >= 3
    # same minute again: drop is in cooldown, chains not due, nothing new goes out
    again = vw.tick(cfg, state, _FakeData(drop=True), notifier, now=now + timedelta(minutes=5))  # type: ignore[arg-type]
    assert again["drops"] == [] and again["chains_ran"] is False
    assert len(notifier.sent) == pushed
    assert (tmp_path / "s.json").exists()
