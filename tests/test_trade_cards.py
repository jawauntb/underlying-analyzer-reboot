from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from app import futures_watch as fw
from app import trade_cards as tc
from app import vol_watch as vw


def _q(label: str, last: float, settle: float, high: float, low: float, ago: float) -> fw.Quote:
    return fw.Quote(label, label.split(" ")[0], last, settle, ago, high, low, "t")


def _vx(front: float, back: float) -> list[dict[str, Any]]:
    return [
        {
            "symbol": "VX/V6",
            "expiration": "10/21/2026",
            "last_price": front,
            "prev_settlement": 19.9,
        },
        {
            "symbol": "VX/X6",
            "expiration": "11/18/2026",
            "last_price": back,
            "prev_settlement": 19.2,
        },
    ]


class FakeSource:
    def __init__(self) -> None:
        self.quotes: dict[str, fw.Quote] = {}
        self.vx: list[dict[str, Any]] = []

    def yahoo_quotes(self, symbols: dict[str, str], window_minutes: int) -> dict[str, fw.Quote]:
        del symbols, window_minutes
        return dict(self.quotes)

    def vx_quotes(self) -> list[dict[str, Any]]:
        return list(self.vx)


class FakePusher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def push(self, title: str, body: str, priority: str = "default", _payload: Any = None) -> None:
        self.sent.append((title, body, priority))


class _FakeData:
    """Vol-watch data stub: tame daily closes, a drop on the intraday tape."""

    def daily_closes(self, _ticker: str, _period: str = "2y") -> pd.Series:
        return pd.Series([100 + (i % 7) * 0.8 for i in range(300)])

    def intraday_closes(self, ticker: str) -> pd.Series:
        start = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)
        idx = pd.DatetimeIndex([start + timedelta(minutes=5 * i) for i in range(6)])
        if ticker == "VIX":
            return pd.Series([18, 18, 19, 20, 20.5, 20.6], index=idx)
        return pd.Series([600, 601, 599, 597, 595, 595], index=idx)

    def vix_level(self) -> float:
        return 18.0

    def chain_metrics(self, ticker: str, _spot: float, _today: Any) -> dict[str, Any]:
        iv = 25.0 if ticker == "XSP" else 45.0
        return {"iv30": iv, "skew": 3.0, "expiries": ["2026-11-20"], "samples": [(30.0, iv)]}

    def zero_dte(self, _ticker: str, _spot: float, _today: Any) -> None:
        return None


def _quote(strike: float, mid: float, iv: float, delta: float) -> vw.Quote:
    return vw.Quote(strike, mid, iv, delta)


def test_structure_labels_and_ev_lines() -> None:
    long_put = tc.Structure(
        "XSP", "2026-10-19", 0.2, "put", _quote(770, 0.76, 0.13, -0.41), None, 770.5
    )
    assert long_put.label() == "buy 1x 770p (0dte)"
    assert long_put.ev_line() == "debit $76, breakeven 769.24, delta -0.41 ≈ p(itm) 41%"
    assert tc.theta_line(long_put).startswith("-15.83 $/hr (0dte, 4.8h left)")
    credit = tc.Structure(
        "XSP",
        "2026-10-19",
        0.2,
        "call",
        _quote(775, 0.20, 0.12, 0.12),
        _quote(772, 0.58, 0.13, 0.28),
        770.5,
    )
    assert credit.label() == "sell 1x 772/775 c credit spread (0dte)"
    assert credit.width == 3.0 and credit.net == pytest.approx(38.0)
    assert credit.ev_line() == "credit $38, max loss $262, p(itm) 28%, ev -46"
    debit = tc.Structure(
        "TSLA",
        "2026-11-20",
        32.0,
        "call",
        _quote(360, 14.0, 0.48, 0.52),
        _quote(380, 6.0, 0.47, 0.26),
        360.0,
    )
    assert debit.label() == "buy 1x 360/380 c debit spread (2026-11-20)"
    assert debit.ev_line() == "debit $800, width $2000, needs p(win) > 40%; long delta 52%"
    assert tc.theta_line(debit).endswith("$/day")


def test_card_renders_his_template_in_order() -> None:
    card = tc.Card("XSP", "buy 1x 770p (0dte)", "c", "e", "today", "t", "h", "r", "x")
    lines = card.render().splitlines()
    assert [line.split(":")[0] for line in lines] == [
        "ticker",
        "Open",
        "Close",
        "EV",
        "Timeframe",
        "Theta",
        "Hoping For",
        "Reason",
        "additional context",
    ]


class FakePicker:
    """Returns fixed structures so the builder's branching can be tested without a chain."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str, str, float]] = []

    def _check(self) -> None:
        if self.fail:
            raise RuntimeError("chain down")

    def long_option(
        self, ticker: str, kind: str, days: float, _delta: float = 0.45
    ) -> tc.Structure:
        self.calls.append(("long", ticker, kind, days))
        self._check()
        return tc.Structure(
            ticker, "2026-10-19", 0.2, kind, _quote(770, 0.76, 0.13, -0.41), None, 770.5
        )

    def credit_spread(
        self, ticker: str, kind: str, days: float, _short_delta: float = 0.25
    ) -> tc.Structure:
        self.calls.append(("credit", ticker, kind, days))
        self._check()
        return tc.Structure(
            ticker,
            "2026-10-19",
            0.2,
            kind,
            _quote(765, 0.2, 0.14, -0.12),
            _quote(768, 0.58, 0.14, -0.28),
            770.5,
        )

    def debit_spread(self, ticker: str, kind: str, days: float) -> tc.Structure:
        self.calls.append(("debit", ticker, kind, days))
        self._check()
        return tc.Structure(
            ticker,
            "2026-11-20",
            32.0,
            kind,
            _quote(360, 14.0, 0.48, 0.52),
            _quote(380, 6.0, 0.47, 0.26),
            360.0,
        )


def test_es_level_cards_pick_the_phase() -> None:
    picker = FakePicker()
    builder = tc.CardBuilder(picker)  # type: ignore[arg-type]
    calm = {"vix": 18.25, "vx_front": 17.45, "vx_back": 18.3, "vx_hour_move": 1.2, "es_change": 0.6}
    title, body = builder.es_level(7740, "below", 7738.5, calm)
    assert title == "ES < 7740" and "Open: buy 1x 770p (0dte)" in body
    assert "Hoping For: ES follows through 7740 to 7720" in body
    assert "additional context: VIX 18.25; VX 17.45/18.30 contango; ES +0.60% vs settle" in body
    spiked = dict(calm, vx_hour_move=9.5)
    title, body = builder.es_level(7720, "below", 7718.0, spiked)
    assert "Open: sell 1x 768/765 p credit spread (0dte)" in body and "gamma is priced" in body
    title, body = builder.es_level(7800, "above", 7801.0, calm)
    assert title == "ES > 7800" and picker.calls[-1] == ("credit", "XSP", "call", 0)
    # chain down: the card still goes out with the reason intact
    down = tc.CardBuilder(FakePicker(fail=True))  # type: ignore[arg-type]
    title, body = down.es_level(7740, "below", 7738.5, calm)
    assert "Open: n/a (chain unavailable)" in body and "Reason: ES crossed below 7740" in body


def test_vol_signal_cards_sell_rich_and_buy_cheap() -> None:
    picker = FakePicker()
    builder = tc.CardBuilder(picker)  # type: ignore[arg-type]
    m = {"iv30": 47.6, "rv20": 30.0, "spread": 17.6, "iv_rank": None, "skew": 2.4, "vix": 18.2}
    title, body = builder.vol_signal("TSLA", "spread", "high", m)
    assert title == "TSLA implied >> realized" and picker.calls[-1] == ("credit", "TSLA", "put", 30)
    assert "Hoping For: volatility crush" in body and "rank n/a" in body
    title, body = builder.vol_signal("MU", "iv_rank", "low", dict(m, iv_rank=9.0))
    assert title == "MU iv rank cheap" and "on your read" in body
    assert [c[0] for c in picker.calls[-2:]] == ["debit", "debit"]
    zero = {"implied_move_pct": 0.41, "realized_move_pct": -0.05}
    _, body = builder.vol_signal("XSP", "skew", "high", dict(m, zero_dte=zero))
    assert "0dte straddle 0.41% implied vs -0.05% realized" in body
    assert picker.calls[-1] == ("credit", "XSP", "put", 0)


def test_futures_watcher_es_levels_cross_both_ways_with_cooldown(tmp_path: Path) -> None:
    cfg = fw.Config(snapshot_times=(), state_path=tmp_path / "f.json", es_levels=(7740.0, 7800.0))
    source, pusher = FakeSource(), FakePusher()
    builder = tc.CardBuilder(FakePicker())  # type: ignore[arg-type]
    watcher = fw.Watcher(cfg, source, pusher, builder)
    source.vx = _vx(17.45, 18.30)
    at = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)

    def es(last: float) -> None:
        source.quotes = {
            "ES Z6": _q("ES Z6", last, 7704.0, last + 5, last - 5, last),
            "VIX": _q("VIX", 18.25, 19.99, 18.6, 18.1, 18.4),
        }

    es(7750.0)
    watcher.tick(at)  # first observation only records the side
    assert pusher.sent == [] and watcher.state.es_side == {"7740": "above", "7800": "below"}
    es(7738.5)
    watcher.tick(at + timedelta(minutes=5))
    assert [s[0] for s in pusher.sent] == ["ES < 7740"]
    assert "ticker: XSP" in pusher.sent[0][1] and pusher.sent[0][2] == "high"
    es(7742.0)
    watcher.tick(at + timedelta(minutes=10))  # back above inside the 30-minute cooldown: silent
    assert len(pusher.sent) == 1 and watcher.state.es_side["7740"] == "above"
    es(7738.0)
    watcher.tick(at + timedelta(minutes=45))
    assert [s[0] for s in pusher.sent] == ["ES < 7740", "ES < 7740"]
    es(7801.0)
    watcher.tick(at + timedelta(minutes=50))
    assert [s[0] for s in pusher.sent][-1] == "ES > 7800"
    assert fw.State.load(tmp_path / "f.json").es_side == {"7740": "above", "7800": "above"}


def test_vol_tick_uses_cards_when_given(tmp_path: Path) -> None:
    class Cards:
        def vol_signal(
            self, ticker: str, signal: str, band: str, _m: dict[str, Any]
        ) -> tuple[str, str]:
            return f"{ticker} card", f"ticker: {ticker}\nOpen: test {signal} {band}"

        def fast_drop(
            self, proxy: str, _hit: dict[str, Any], _facts: dict[str, Any]
        ) -> tuple[str, str]:
            return f"drop {proxy}", "ticker: XSP\nOpen: drop card"

    cfg = vw.Config(watchlist=["XSP", "MU"], state_path=tmp_path / "s.json")
    notifier = vw.Notifier(cfg)
    now = datetime(2026, 10, 16, 14, 30, tzinfo=UTC)
    vw.tick(cfg, vw.State(), _FakeData(), notifier, now=now, cards=Cards())  # type: ignore[arg-type]
    titles = [s["title"] for s in notifier.sent]
    assert titles == ["drop SPY", "MU card"]  # xsp is gated by |iv30 - vix| > 2
    assert notifier.sent[1]["body"] == "ticker: MU\nOpen: test spread high"


def test_spot_only_cards_for_crypto_and_peso() -> None:
    builder = tc.CardBuilder(FakePicker())  # type: ignore[arg-type]
    title, body = builder.futures_move("ETH-USD", "ETH", -5.8, {"vix": 18.0})
    assert title == "MOVE ETH-USD -5.80%"
    assert "Open: buy $100 ETH spot (max loss $100)" in body and "Theta: 0 (spot)" in body
    _, body = builder.futures_move("SOL-USD", "SOL", 6.0, {})
    assert "Open: none: no defined-risk way to fade" in body
    _, body = builder.futures_move("USD-ARS", "ARS", 2.0, {})
    assert "official peso rate is managed" in body
    _, body = builder.futures_move("HG front", "HG", -1.7, {})
    assert "ticker: COPX" in body
