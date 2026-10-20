from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from app import futures_watch as fw


def test_contract_roll_rules_on_2026_10_19() -> None:
    today = date(2026, 10, 19)
    assert fw.contract_months("ES", today) == [(2026, 12), (2027, 3)]
    assert fw.contract_months("NQ", today) == [(2026, 12), (2027, 3)]
    assert fw.contract_months("GC", today) == [(2026, 12), (2027, 2)]
    assert fw.contract_months("CL", today) == [(2026, 12), (2027, 1)]
    assert fw.yahoo_symbol("ES", 2026, 12) == "ESZ26.CME"
    assert fw.yahoo_symbol("GC", 2027, 2) == "GCG27.CMX"
    assert fw.yahoo_symbol("CL", 2027, 1) == "CLF27.NYM"
    assert fw.contract_label("ES", 2026, 12) == "ES Z6"
    # a week before the december third friday (dec 18) es rolls to march
    assert fw.contract_months("ES", date(2026, 12, 11)) == [(2027, 3), (2027, 6)]
    assert fw.contract_months("ES", date(2026, 12, 10)) == [(2026, 12), (2027, 3)]


def test_monthly_vx_filters_weeklies_and_expired() -> None:
    rows = [
        {
            "symbol": "VX/V6",
            "expiration": "10/21/2026",
            "last_price": 17.45,
            "prev_settlement": 19.9,
        },
        {"symbol": "VX43/V6", "expiration": "10/28/2026", "last_price": 0.0},
        {
            "symbol": "VX/X6",
            "expiration": "11/18/2026",
            "last_price": 18.3,
            "prev_settlement": 19.24,
        },
        {"symbol": "VX/U6", "expiration": "09/16/2026", "last_price": 16.0},
        {"symbol": "VX/Z6", "expiration": "12/16/2026", "last_price": 18.47},
    ]
    monthly = fw.monthly_vx(rows, date(2026, 10, 19))
    assert [r["_label"] for r in monthly] == ["VX V6", "VX X6", "VX Z6"]
    assert fw.monthly_vx(rows, date(2026, 10, 21))[0]["_label"] == "VX X6"


def test_futures_hours() -> None:
    assert fw.futures_open(datetime(2026, 10, 19, 8, 0, tzinfo=UTC))  # monday 04:00 et
    assert not fw.futures_open(datetime(2026, 10, 19, 21, 30, tzinfo=UTC))  # 17:30 et break
    assert fw.futures_open(datetime(2026, 10, 19, 22, 30, tzinfo=UTC))  # 18:30 et
    assert not fw.futures_open(datetime(2026, 10, 17, 15, 0, tzinfo=UTC))  # saturday
    assert not fw.futures_open(datetime(2026, 10, 18, 20, 0, tzinfo=UTC))  # sunday 16:00 et
    assert fw.futures_open(datetime(2026, 10, 18, 23, 0, tzinfo=UTC))  # sunday 19:00 et
    assert not fw.futures_open(datetime(2026, 10, 23, 21, 30, tzinfo=UTC))  # friday 17:30 et


def _q(label: str, last: float, settle: float, high: float, low: float, ago: float) -> fw.Quote:
    return fw.Quote(label, label.split(" ")[0], last, settle, ago, high, low, "t")


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


def _watcher(
    tmp_path: Path, times: tuple[str, ...] = ("08:30",)
) -> tuple[fw.Watcher, FakeSource, FakePusher]:
    cfg = fw.Config(snapshot_times=times, state_path=tmp_path / "f.json")
    source, pusher = FakeSource(), FakePusher()
    return fw.Watcher(cfg, source, pusher), source, pusher


def test_snapshot_once_per_slot_and_skipped_when_late(tmp_path: Path) -> None:
    watcher, source, pusher = _watcher(tmp_path)
    source.quotes = {
        "ES Z6": _q("ES Z6", 7752.75, 7704.0, 7760.0, 7730.0, 7732.0),
        "ES H7": _q("ES H7", 7829.75, 7780.0, 7835.0, 7800.0, 7801.0),
        "GC Z6": _q("GC Z6", 4163.7, 4181.0, 4170.0, 4160.0, 4165.0),
        "GLD": _q("GLD", 380.08, 382.0, 381.0, 379.5, 380.5),
        "CL Z6": _q("CL Z6", 97.62, 99.0, 98.5, 97.3, 98.0),
        "VIX": _q("VIX", 18.25, 19.99, 18.6, 18.1, 18.4),
        "VIX3M": _q("VIX3M", 19.48, 20.5, 19.6, 19.4, 19.5),
    }
    source.vx = _vx(17.45, 18.30)
    at = datetime(2026, 10, 19, 12, 35, tzinfo=UTC)  # 08:35 et
    watcher.tick(at)
    watcher.tick(at + timedelta(minutes=5))
    snaps = [s for s in pusher.sent if s[0].startswith("futures")]
    assert len(snaps) == 1
    body = snaps[0][1]
    assert body.splitlines()[0] == "ES Z6 7,752.75 (+0.63% vs settle, +0.29% 1h) | ES H7 7,829.75"
    assert "VX V6 17.45 (-12.3% vs settle) | VX X6 18.30 | spread +0.85 contango" in body
    assert "| VIX 18.25 VIX3M 19.48" in body
    assert "GC Z6 4,163.7 (-0.41%) | GLD 380.08 (-0.50%)" in body
    assert "CL Z6 97.62 (-1.39%)" in body
    # a slot the service slept through (more than 90 minutes late) is marked and skipped
    late, _, pusher2 = _watcher(tmp_path, times=("06:30",))
    late.source = source
    late.tick(at)
    assert not [s for s in pusher2.sent if s[0].startswith("futures")]
    assert "2026-10-19 06:30" in late.state.snapshots_sent


def test_move_alert_front_month_only_with_cooldown(tmp_path: Path) -> None:
    watcher, source, pusher = _watcher(tmp_path, times=())
    source.quotes = {
        "ES Z6": _q("ES Z6", 7690.0, 7704.0, 7760.0, 7685.0, 7755.0),  # -0.90% off the hour high
        "ES H7": _q("ES H7", 7760.0, 7780.0, 7835.0, 7755.0, 7830.0),
        "NQ Z6": _q("NQ Z6", 29700.0, 29500.0, 29750.0, 29650.0, 29700.0),  # quiet
    }
    source.vx = _vx(18.9, 18.3)
    at = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
    events = watcher.tick(at)
    moves = [s for s in pusher.sent if s[0].startswith("MOVE")]
    assert len(moves) == 1 and moves[0][0] == "MOVE ES Z6 -0.90% in 60m"
    assert "7,760.00 -> 7,690.00" in moves[0][1] and "VX V6 18.90" in moves[0][1]
    assert moves[0][2] == "high"
    assert [e["kind"] for e in events if e["kind"] == "move"] == ["move"]
    watcher.tick(at + timedelta(minutes=30))
    assert len([s for s in pusher.sent if s[0].startswith("MOVE")]) == 1  # cooldown
    watcher.tick(at + timedelta(minutes=61))
    assert len([s for s in pusher.sent if s[0].startswith("MOVE")]) == 2


def test_term_structure_flip_fires_on_change_only(tmp_path: Path) -> None:
    watcher, source, pusher = _watcher(tmp_path, times=())
    at = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
    source.vx = _vx(17.45, 18.30)
    watcher.tick(at)
    assert not [s for s in pusher.sent if s[0].startswith("VIX TERM")]  # first read sets state
    source.vx = _vx(19.0, 18.5)
    watcher.tick(at + timedelta(minutes=5))
    terms = [s for s in pusher.sent if s[0].startswith("VIX TERM")]
    assert terms == [("VIX TERM: backwardation", terms[0][1], "high")]
    assert "VX V6 19.00 vs VX X6 18.50 (spread -0.50)" in terms[0][1]
    watcher.tick(at + timedelta(minutes=10))
    assert len([s for s in pusher.sent if s[0].startswith("VIX TERM")]) == 1
    source.vx = _vx(18.0, 18.5)
    watcher.tick(at + timedelta(minutes=15))
    assert [s[0] for s in pusher.sent if s[0].startswith("VIX TERM")][
        -1
    ] == "VIX TERM: back to contango"
    # the vx ring buffer gives a trailing-hour move once it has more than one sample
    assert watcher.state.vx_history["VX V6"][-1][1] == 18.0
    assert len(watcher.state.vx_history["VX V6"]) == 4


def test_overnight_alert_outside_cash_session_once(tmp_path: Path) -> None:
    watcher, source, pusher = _watcher(tmp_path, times=())
    source.quotes = {
        "ES Z6": _q("ES Z6", 7600.0, 7704.0, 7610.0, 7590.0, 7600.0)
    }  # -1.35% vs settle
    night = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)  # 02:00 et tuesday
    watcher.tick(night)
    watcher.tick(night + timedelta(minutes=5))
    overs = [s for s in pusher.sent if s[0].startswith("OVERNIGHT")]
    assert len(overs) == 1 and overs[0][0] == "OVERNIGHT ES Z6 -1.35% vs settle"
    watcher.tick(datetime(2026, 10, 20, 15, 0, tzinfo=UTC))  # cash session: no overnight alerts
    assert len([s for s in pusher.sent if s[0].startswith("OVERNIGHT")]) == 1


def test_contracts_chosen_once_per_day_and_persisted(tmp_path: Path) -> None:
    watcher, source, _ = _watcher(tmp_path, times=())
    source.vx = _vx(17.45, 18.30)
    watcher.tick(datetime(2026, 10, 19, 15, 0, tzinfo=UTC))
    assert watcher.state.contracts["ES Z6"] == "ESZ26.CME"
    assert watcher.state.contracts["CL F7"] == "CLF27.NYM"
    assert watcher.state.contracts["VIX3M"] == "^VIX3M"
    reloaded = fw.State.load(tmp_path / "f.json")
    assert reloaded.contracts == watcher.state.contracts and reloaded.backwardation is False


def test_commodity_fx_crypto_moves_and_crypto_on_weekends(tmp_path: Path) -> None:
    watcher, source, pusher = _watcher(tmp_path, times=())
    assert watcher.cfg.move_pct["ETH"] == 3.0 and watcher.cfg.move_pct["HG"] == 1.5
    source.quotes = {
        "HG front": fw.Quote("HG front", "HG", 6.50, 6.62, 6.60, 6.61, 6.49, "t"),  # -1.66%
        "BRL-USD": fw.Quote("BRL-USD", "BRL", 0.1994, 0.2000, 0.1995, 0.1996, 0.1993, "t"),
        "ETH-USD": fw.Quote("ETH-USD", "ETH", 2300.0, 2441.0, 2440.0, 2442.0, 2299.0, "t"),
    }
    source.vx = _vx(17.45, 18.30)
    watcher.tick(datetime(2026, 10, 20, 15, 0, tzinfo=UTC))
    titles = sorted(s[0] for s in pusher.sent if s[0].startswith("MOVE"))
    assert titles == ["MOVE ETH-USD -5.81% in 60m", "MOVE HG front -1.66% in 60m"]
    assert watcher.state.contracts["BRL-USD"] == "BRLUSD=X"
    assert watcher.state.contracts["USD-ARS"] == "ARS=X"
    assert watcher.state.contracts["ZS front"] == "ZS=F"
    # saturday: futures closed, crypto still read and alerted
    sat = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)
    source.quotes = {"SOL-USD": fw.Quote("SOL-USD", "SOL", 98.0, 105.8, 105.0, 105.5, 97.9, "t")}
    watcher.tick(sat)
    assert any(s[0].startswith("MOVE SOL-USD") for s in pusher.sent)


def test_snapshot_lists_ags_fx_and_crypto() -> None:
    quotes = {
        "ZS front": fw.Quote("ZS front", "ZS", 1249.0, 1240.0, None, None, None, "t"),
        "BRL-USD": fw.Quote("BRL-USD", "BRL", 0.1994, 0.2000, None, None, None, "t"),
        "USD-ARS": fw.Quote("USD-ARS", "ARS", 1525.0, 1520.0, None, None, None, "t"),
        "ETH-USD": fw.Quote("ETH-USD", "ETH", 2441.27, 2400.0, None, None, None, "t"),
    }
    _, body = fw.render_snapshot(quotes, "08:30 ET")
    assert "ZS 1,249.00 (+0.73%)" in body
    assert "BRL-USD 0.1994 (-0.30%) | USD-ARS 1,525.00 (+0.33%) (24h change; ARS is the official" in body
    assert "ETH 2,441.27 (+1.72%)" in body
