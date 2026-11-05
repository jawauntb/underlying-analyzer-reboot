"""Fixes from the 2026-11-04 alert backtest (HVega reports/alerts/2026-11-04-volwatch-backtest.md).

Each test pins one behaviour the backtest asked for. The production values come from
``Config.from_env`` (all proposed, none fitted); the dataclass defaults keep the old behaviour.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app import futures_watch as fw
from app import trade_cards as tc
from app import vol_watch as vw

T0 = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)


def _cfg(tmp_path: Path, **kw: Any) -> vw.Config:
    base: dict[str, Any] = {
        "watchlist": ["SDGR"],
        "state_path": tmp_path / "s.json",
        "confirm_passes": 2,
        "spread_rich_exit": 6.0,
        "spread_cheap_exit": -1.0,
        "skew_exit": 10.0,
    }
    base.update(kw)
    return vw.Config(**base)


def _m(spread: float | None, skew: float | None = 2.0) -> dict[str, Any]:
    return {"iv_rank": None, "spread": spread, "skew": skew}


def test_from_env_turns_the_fixes_on(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in list(__import__("os").environ):
        if k.startswith("VOL_WATCH_"):
            monkeypatch.delenv(k)
    cfg = vw.Config.from_env()
    assert cfg.confirm_passes == 2
    assert (cfg.spread_rich_exit, cfg.spread_cheap_exit, cfg.skew_exit) == (6.0, -1.0, 10.0)
    assert cfg.max_iv_jump == 0.25 and cfg.rv_forecast is True
    assert cfg.no_signal == ("VIX",) and cfg.push_ledger is not None
    monkeypatch.setenv("VOL_WATCH_PUSH_LEDGER", "off")
    monkeypatch.setenv("VOL_WATCH_RV_FORECAST", "rv20")
    monkeypatch.setenv("VOL_WATCH_NO_SIGNAL", "")
    cfg = vw.Config.from_env()
    assert cfg.push_ledger is None and cfg.rv_forecast is False and cfg.no_signal == ()


def test_a_one_pass_spike_never_pushes(tmp_path: Path) -> None:
    cfg, state = _cfg(tmp_path), vw.State()
    assert vw.crossings("SDGR", _m(1.0), state, cfg, T0) == []
    # SDGR 2026-11-03: spread +47 for one pass, then back to +1.5
    assert vw.crossings("SDGR", _m(47.2), state, cfg, T0 + timedelta(minutes=15)) == []
    assert vw.crossings("SDGR", _m(1.5), state, cfg, T0 + timedelta(minutes=30)) == []
    assert state.pending == {} and state.signals["SDGR:spread"] == "mid"


def test_a_band_that_holds_two_passes_pushes_once(tmp_path: Path) -> None:
    cfg, state = _cfg(tmp_path), vw.State()
    vw.crossings("SDGR", _m(1.0), state, cfg, T0)
    assert vw.crossings("SDGR", _m(9.0), state, cfg, T0 + timedelta(minutes=15)) == []
    fired = vw.crossings("SDGR", _m(9.5), state, cfg, T0 + timedelta(minutes=30))
    assert [(a["signal"], a["band"]) for a in fired] == [("spread", "high")]
    assert vw.crossings("SDGR", _m(9.1), state, cfg, T0 + timedelta(minutes=45)) == []


def test_hysteresis_stops_a_name_hovering_at_the_threshold(tmp_path: Path) -> None:
    cfg, state = _cfg(tmp_path), vw.State()
    vw.crossings("DRAM", _m(1.0), state, cfg, T0)
    vw.crossings("DRAM", _m(8.4), state, cfg, T0 + timedelta(minutes=15))
    assert vw.crossings("DRAM", _m(8.2), state, cfg, T0 + timedelta(minutes=30))
    pushes = 0
    for i, spread in enumerate([7.6, 8.3, 7.1, 8.6, 6.4, 8.1]):  # dips under 8, never under 6
        t = T0 + timedelta(minutes=45 + 15 * i)
        pushes += len(vw.crossings("DRAM", _m(spread), state, cfg, t))
    assert pushes == 0 and state.signals["DRAM:spread"] == "high"
    vw.crossings("DRAM", _m(5.0), state, cfg, T0 + timedelta(hours=3))
    assert state.signals["DRAM:spread"] == "mid"


def test_old_behaviour_is_the_dataclass_default(tmp_path: Path) -> None:
    cfg, state = vw.Config(watchlist=["MU"], state_path=tmp_path / "s.json"), vw.State()
    vw.crossings("MU", _m(1.0), state, cfg, T0)
    assert vw.crossings("MU", _m(9.0), state, cfg, T0 + timedelta(minutes=15))


class _Data:
    def __init__(self, iv: list[float]) -> None:
        self.iv = list(iv)

    def daily_closes(self, _t: str, _p: str = "2y") -> pd.Series:
        return pd.Series([28 + (i % 5) * 0.3 for i in range(60)])

    def chain_metrics(self, _t: str, _s: float, _d: date) -> dict[str, Any]:
        return {"iv30": self.iv.pop(0), "skew": None, "expiries": []}

    def zero_dte(self, *_a: Any) -> None:
        return None


def test_an_iv_jump_waits_a_pass_and_is_kept_if_it_repeats(tmp_path: Path) -> None:
    cfg = vw.Config(watchlist=["SDGR"], state_path=tmp_path / "s.json", max_iv_jump=0.25)
    state = vw.State()
    data = _Data([99.0, 145.8, 100.2, 140.0, 141.0])
    day = date(2026, 11, 3)
    state.last_metrics["SDGR"] = vw.compute_metrics("SDGR", data, state, cfg, day, None)  # type: ignore[arg-type]
    with pytest.raises(vw.SkipTicker, match="jumped"):
        vw.compute_metrics("SDGR", data, state, cfg, day, None)  # type: ignore[arg-type]
    m = vw.compute_metrics("SDGR", data, state, cfg, day, None)  # type: ignore[arg-type]
    assert m["iv30"] == 100.2 and state.iv_suspect == {}
    state.last_metrics["SDGR"] = m
    with pytest.raises(vw.SkipTicker):
        vw.compute_metrics("SDGR", data, state, cfg, day, None)  # type: ignore[arg-type]
    assert vw.compute_metrics("SDGR", data, state, cfg, day, None)["iv30"] == 141.0  # type: ignore[arg-type]


def test_har_forecast_recovers_a_constant_vol_and_needs_history() -> None:
    rng = np.random.default_rng(4)
    rets = rng.normal(0.0, 0.30 / math.sqrt(252), 600)
    closes = pd.Series(100 * np.exp(np.cumsum(rets)))
    fc = vw.har_rv_forecast(closes)
    assert fc is not None and 25.0 < fc < 35.0
    assert vw.har_rv_forecast(closes.tail(100)) is None


def test_har_forecast_lets_an_old_gap_fade_while_rv20_still_holds_it() -> None:
    rng = np.random.default_rng(9)
    rets = rng.normal(0.0, 0.25 / math.sqrt(252), 520)
    rets[-15] = -0.12  # one gap three weeks ago, like EWZ on 2026-10-26
    closes = pd.Series(100 * np.exp(np.cumsum(rets)))
    rv20 = vw.realized_vol(closes, 20)
    fc = vw.har_rv_forecast(closes)
    assert rv20 is not None and fc is not None and fc < rv20


def test_spread_uses_the_forecast_when_enabled(tmp_path: Path) -> None:
    class Long(_Data):
        def daily_closes(self, _t: str, _p: str = "2y") -> pd.Series:
            rng = np.random.default_rng(1)
            return pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 500))))

    cfg = vw.Config(watchlist=["MU"], state_path=tmp_path / "s.json", rv_forecast=True)
    m = vw.compute_metrics("MU", Long([40.0]), vw.State(), cfg, date(2026, 11, 3), None)  # type: ignore[arg-type]
    assert m["spread_basis"] == "har" and m["spread"] == round(40.0 - m["rv_fc"], 2)


def test_vix_gets_no_vol_signal(tmp_path: Path) -> None:
    cfg = vw.Config(watchlist=["VIX"], state_path=tmp_path / "s.json", no_signal=("VIX",))
    notifier = vw.Notifier(cfg)

    class Boom:
        def vix_level(self) -> float:
            return 17.0

        def daily_closes(self, *_a: Any) -> pd.Series:
            raise AssertionError("VIX must be skipped before any fetch")

    fired = vw.run_chain_pass(cfg, vw.State(), Boom(), notifier, date(2026, 11, 3), T0)  # type: ignore[arg-type]
    assert fired == [] and notifier.sent == []


def test_every_push_lands_in_the_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "pushes.jsonl"
    cfg = vw.Config(watchlist=[], state_path=tmp_path / "s.json", push_ledger=ledger)
    notifier = vw.Notifier(cfg)
    notifier.push("SDGR implied >> realized", "body", "default", {"ticker": "SDGR", "at": T0})
    notifier.push("MOVE 6L front +1.18%", "card", "high")
    rows = [json.loads(x) for x in ledger.read_text().splitlines()]
    assert [r["title"] for r in rows] == ["SDGR implied >> realized", "MOVE 6L front +1.18%"]
    assert rows[0]["payload"]["ticker"] == "SDGR" and rows[1]["priority"] == "high"


def test_move_cards_are_context_only_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(tc.FADE_ENV, raising=False)
    builder = tc.CardBuilder(object())  # type: ignore[arg-type]  # no chain is ever read
    for label, root, move in (
        ("6L front", "6L", 1.18),
        ("NQ Z6", "NQ", -0.9),
        ("ETH-USD", "ETH", -5.8),
    ):
        title, body = builder.futures_move(label, root, move, {"vix": 18.0})
        assert title.startswith(f"MOVE {label}")
        assert "Open: none: context only" in body and "Hoping For: context only" in body
    assert "ticker: EWZ" in builder.futures_move("6L front", "6L", 1.18, {})[1]
    monkeypatch.setenv(tc.FADE_ENV, "1")
    assert tc.CardBuilder(object()).fade_trades is True  # type: ignore[arg-type]


def test_6l_and_brl_fire_one_move_between_them(tmp_path: Path) -> None:
    class Src:
        def yahoo_quotes(self, *_a: Any) -> dict[str, fw.Quote]:
            return {
                "6L front": fw.Quote("6L front", "6L", 0.1820, 0.1800, 0.1790, 0.1821, 0.1790, "t"),
                "BRL-USD": fw.Quote("BRL-USD", "BRL", 0.1822, 0.1802, 0.1792, 0.1823, 0.1791, "t"),
            }

        def vx_quotes(self) -> list[dict[str, Any]]:
            return []

    class Pusher:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def push(self, title: str, *_a: Any) -> None:
            self.sent.append(title)

    pusher = Pusher()
    watcher = fw.Watcher(
        fw.Config(snapshot_times=(), state_path=tmp_path / "f.json"), Src(), pusher
    )
    watcher.tick(datetime(2026, 10, 27, 13, 0, tzinfo=UTC))
    watcher.tick(datetime(2026, 10, 27, 13, 30, tzinfo=UTC))
    assert pusher.sent == ["MOVE 6L front +1.68% in 60m"]
