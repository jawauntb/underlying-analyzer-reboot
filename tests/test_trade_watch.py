from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app import trade_watch as tw

INST = "https://api.robinhood.com/options/instruments/"


def _inst(strike: float, kind: str, exp: str = "2026-10-19") -> dict[str, Any]:
    return {
        "chain_symbol": "XSP",
        "strike_price": f"{strike:.4f}",
        "type": kind,
        "expiration_date": exp,
    }


INSTRUMENTS = {
    f"{INST}{k}/": v
    for k, v in {
        "760p": _inst(760, "put"),
        "762p": _inst(762, "put"),
        "778c": _inst(778, "call"),
        "780c": _inst(780, "call"),
        "755p": _inst(755, "put"),
        "757p": _inst(757, "put"),
        "783c": _inst(783, "call"),
        "785c": _inst(785, "call"),
        "766p": _inst(766, "put"),
    }.items()
}


def lookup(url: str) -> dict[str, Any]:
    return INSTRUMENTS[url]


def _leg(name: str, side: str, effect: str, price: float, qty: float = 1.0) -> dict[str, Any]:
    return {
        "option": f"{INST}{name}/",
        "side": side,
        "position_effect": effect,
        "ratio_quantity": "1",
        "executions": [{"price": f"{price:.2f}", "quantity": f"{qty:.5f}", "timestamp": "t"}],
    }


def _order(
    oid: str,
    ts: str,
    direction: str,
    premium: float,
    legs: list[dict],
    state: str = "filled",
    qty: float = 1.0,
    processed: float | None = None,
) -> dict[str, Any]:
    done = qty if processed is None else processed
    return {
        "id": oid,
        "state": state,
        "direction": direction,
        "premium": f"{premium:.2f}",
        "processed_premium": f"{premium * done:.2f}" if done else "0.00",
        "quantity": f"{qty:.5f}",
        "processed_quantity": f"{done:.5f}",
        "created_at": ts,
        "updated_at": ts,
        "regulatory_fees": "0.00",
        "legs": legs if done else [dict(leg, executions=[]) for leg in legs],
    }


# the real tape from 2026-10-19 (the official Robinhood order shape, ids kept, no account
# number): two 766P buys at -$62 and -$88, a call condor and a put condor opened for
# debits and closed leg-pair by leg-pair for -$6 net, two cancelled orders.
TAPE = json.loads((Path(__file__).parent / "fixtures" / "xsp_orders_2026-10-19.json").read_text())
BY_ID = {o["id"]: o for o in TAPE}
PUT_CANCEL = BY_ID["6ad61b0d-ea91-4078-a4e7-32dd30e8d8e1"]
PUT_1 = BY_ID["6ad61b4d-78e1-4b77-9f16-bdc6f66bac4f"]  # 1 @ 0.62
PUT_2 = BY_ID["6ad61b9c-063b-44e4-a6dc-26b4feb94185"]  # 2 @ 0.44
EXIT_CANCELED = BY_ID["6ad61c9a-a538-4d72-bb0b-11dcdc9af6b1"]
EXIT_WORKING = dict(EXIT_CANCELED, state="confirmed", updated_at="2026-10-19T13:35:22.299882Z")
CALL_CONDOR = BY_ID["6ad61d00-ddd4-4917-916a-7025b2de6cbd"]
PUT_CONDOR = BY_ID["6ad61d15-15d7-4e8b-b0d1-ea1b215f099a"]
CLOSES = [
    BY_ID["6ad61ddd-79d5-4fb8-b0d4-a9696a77c782"],  # short call spread -17
    BY_ID["6ad61df0-8845-4a07-bef8-8d3b463c79d2"],  # long call spread +35
    BY_ID["6ad61e36-d631-4141-a357-85b690ffdb02"],  # short put spread -23
    BY_ID["6ad61e60-051d-43f2-ae29-0a249acafec5"],  # long put spread +43
]


CONDOR_LEGACY = _order(
    "L-open",
    "2026-10-19T13:35:00Z",
    "credit",
    95.0,
    [
        _leg("762p", "sell", "open", 1.50),
        _leg("760p", "buy", "open", 0.90),
        _leg("778c", "sell", "open", 1.20),
        _leg("780c", "buy", "open", 0.85),
    ],
)


class FakeBroker:
    def __init__(self) -> None:
        self.raw: list[dict[str, Any]] = []
        self.mark_table: dict[str, float | None] = {}

    def orders(self) -> list[tw.Order]:
        return [tw.normalize_option_order(r, lookup) for r in self.raw]

    def marks(self, keys: list[str]) -> dict[str, float | None]:
        return {k: self.mark_table.get(k) for k in keys}


class FakePusher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def push(self, title: str, body: str, priority: str = "default", _payload: Any = None) -> None:
        self.sent.append((title, body, priority))


def _watcher(tmp_path: Path) -> tuple[tw.Watcher, FakeBroker, FakePusher]:
    cfg = tw.Config(state_path=tmp_path / "state.json", ledger_csv=tmp_path / "ledger.csv")
    broker, pusher = FakeBroker(), FakePusher()
    return tw.Watcher(cfg, broker, pusher, tw.Ledger(cfg)), broker, pusher


def test_normalize_official_order_shape_reads_inline_legs() -> None:
    cache: dict[str, dict[str, Any]] = {}
    order = tw.normalize_option_order(CALL_CONDOR, lookup, cache)
    assert order.side == "OPEN" and order.qty == 1.0 and order.net == -24.0
    assert [leg.key for leg in order.legs] == [
        "XSP 774C 2026-10-19",
        "XSP 773C 2026-10-19",
        "XSP 772C 2026-10-19",
        "XSP 771C 2026-10-19",
    ]
    assert order.legs[3].price == 1.23 and order.unit_price == pytest.approx(0.24)
    assert tw.spread_width(order.legs) == 3.0
    assert tw.max_loss_at_entry(order) == 24.0  # a debit condor risks the debit
    assert len(cache) == 4  # inline legs seed the instrument cache for marks
    put = tw.normalize_option_order(PUT_2, lookup)
    assert put.side == "BUY" and put.qty == 2.0 and put.net == -88.0
    assert put.unit_price == pytest.approx(0.44) and tw.max_loss_at_entry(put) == 88.0
    working = tw.normalize_option_order(EXIT_WORKING, lookup)
    assert working.state == "working" and working.qty == 0 and working.unit_price is None
    legacy = tw.normalize_option_order(CONDOR_LEGACY, lookup)
    assert legacy.legs[0].key == "XSP 762P 2026-10-19"  # brokerage shape via instrument url


def test_replay_of_the_day_matches_the_ledger(tmp_path: Path) -> None:
    watcher, broker, pusher = _watcher(tmp_path)
    t = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)  # 10:00 et: past the 09:31 snapshot
    tape = [PUT_CANCEL, PUT_1, PUT_2, EXIT_WORKING, CALL_CONDOR, PUT_CONDOR, *CLOSES]
    kernels = []
    for raw in tape:
        broker.raw.append(raw)
        watcher.poll(t)
        kernels.append(round(watcher.state.kernel, 2))
    broker.raw[3] = EXIT_CANCELED  # the 1.20 exit on the puts is cancelled at 09:44
    watcher.poll(t)
    state = watcher.state
    assert state.realized == pytest.approx(-6.0)
    assert state.open_max_loss == pytest.approx(150.0)
    assert state.kernel == pytest.approx(156.0)
    assert list(state.positions) == ["XSP 766P 2026-10-19"]
    assert state.positions["XSP 766P 2026-10-19"].qty == 3.0
    assert state.positions["XSP 766P 2026-10-19"].cost == pytest.approx(-150.0)
    # kernel path: 62, 150, 150, 174, 194; then each close drops half a condor and books p&l
    assert kernels[:5] == [0.0, 62.0, 150.0, 150.0, 174.0]
    assert kernels[5] == 194.0
    assert kernels[-1] == 156.0
    fills = [s for s in pusher.sent if s[0].startswith("FILL")]
    assert len(fills) == 8
    assert fills[0][1].startswith("FILL BUY 1 buy 1x XSP 766P 2026-10-19 @ 0.62 | fees 0.00")
    assert "net debit -$62.00" in fills[0][1]
    assert fills[2][1].startswith(
        "FILL OPEN 1 buy 1x XSP 774C 2026-10-19 / sell 1x XSP 773C 2026-10-19 / "
        "sell 1x XSP 772C 2026-10-19 / buy 1x XSP 771C 2026-10-19 @ 0.24"
    )
    assert "kernel $156" in fills[-1][1]
    cancels = [s for s in pusher.sent if s[0].startswith("CANCELED")]
    assert [c[0] for c in cancels] == ["CANCELED BUY XSP", "CANCELED SELL XSP"]
    assert "sell 1x XSP 766P 2026-10-19" in cancels[1][1]
    assert [s[0] for s in pusher.sent if "snapshot" in s[0]] == ["09:31 snapshot"]
    assert not [s for s in pusher.sent if s[0].startswith("KERNEL")]  # peak was $194 < $250
    before = len(pusher.sent)
    watcher.poll(t)  # re-polling the same tape books and pushes nothing twice
    assert len(pusher.sent) == before and watcher.state.kernel == pytest.approx(156.0)
    rows = (tmp_path / "ledger.csv").read_text().splitlines()
    assert rows[0] == ",".join(tw.LEDGER_COLUMNS)
    assert len(rows) == 9
    last = rows[-1].split(",")
    assert last[1] == "6ad61e60-051d-43f2-ae29-0a249acafec5" and last[-3:-1] == ["-6.0", "156.0"]


def test_canceled_exit_is_pushed_once(tmp_path: Path) -> None:
    watcher, broker, pusher = _watcher(tmp_path)
    t = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
    broker.raw = [PUT_1, EXIT_WORKING]
    watcher.poll(t)
    assert not [s for s in pusher.sent if s[0].startswith("CANCELED")]
    broker.raw = [PUT_1, EXIT_CANCELED]
    watcher.poll(t)
    watcher.poll(t)
    cancels = [s for s in pusher.sent if s[0].startswith("CANCELED")]
    assert len(cancels) == 1 and "sell 1x XSP 766P 2026-10-19" in cancels[0][1]
    assert watcher.state.realized == pytest.approx(0.0) and watcher.state.kernel == 62.0
    # a cancelled order the watcher never saw working (placed and pulled before a poll)
    # is still reported once, since a silent disappearance is the failure mode
    broker.raw = [PUT_1, EXIT_CANCELED, PUT_CANCEL]
    watcher.poll(t)
    assert len([s for s in pusher.sent if s[0].startswith("CANCELED")]) == 2


def test_kernel_steps_and_day_doubled_fire_once(tmp_path: Path) -> None:
    watcher, broker, pusher = _watcher(tmp_path)
    t = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
    big = _order("BIG", "2026-10-19T14:00:00Z", "debit", 410.0, [_leg("766p", "buy", "open", 4.10)])
    broker.raw = [big]
    watcher.poll(t)
    titles = [s[0] for s in pusher.sent]
    assert any(x.startswith("KERNEL 50%") for x in titles)
    assert any(x.startswith("KERNEL 80%") for x in titles)
    assert not any(x.startswith("KERNEL 100%") for x in titles)
    sell = _order(
        "SELL", "2026-10-19T15:30:00Z", "credit", 950.0, [_leg("766p", "sell", "close", 9.50)]
    )
    broker.raw = [big, sell]
    watcher.poll(t)
    watcher.poll(t)
    assert watcher.state.realized == pytest.approx(540.0)
    assert [s[0] for s in pusher.sent].count("DAY DOUBLED, done for the day") == 1
    assert watcher.state.kernel == 0.0


def test_close_summary_once_after_1605_and_ledgered(tmp_path: Path) -> None:
    watcher, broker, pusher = _watcher(tmp_path)
    broker.raw = [PUT_1]
    broker.mark_table = {"XSP 766P 2026-10-19": 1.00}
    watcher.poll(datetime(2026, 10, 19, 20, 6, tzinfo=UTC))  # 16:06 et
    watcher.poll(datetime(2026, 10, 19, 20, 7, tzinfo=UTC))
    summaries = [s for s in pusher.sent if s[0] == "close summary"]
    assert len(summaries) == 1
    assert "realized -$0.00" in summaries[0][1] or "realized +$0.00" in summaries[0][1]
    assert "unrealized +$38.00" in summaries[0][1]  # long 1 @ 0.62, mark 1.00
    rows = (tmp_path / "ledger.csv").read_text().splitlines()
    assert rows[-1].split(",")[3] == "SUMMARY"


def test_state_survives_restart(tmp_path: Path) -> None:
    watcher, broker, pusher = _watcher(tmp_path)
    broker.raw = [PUT_1, PUT_2]
    watcher.poll(datetime(2026, 10, 19, 15, 0, tzinfo=UTC))
    again, broker2, pusher2 = _watcher(tmp_path)
    broker2.raw = [PUT_1, PUT_2]
    again.poll(datetime(2026, 10, 19, 15, 1, tzinfo=UTC))
    assert pusher2.sent == [] and again.state.kernel == 150.0  # 62 + 88 carried over


def test_window_is_0925_to_1615_et() -> None:
    assert tw.in_window(datetime(2026, 10, 19, 13, 26, tzinfo=UTC))  # 09:26 et
    assert not tw.in_window(datetime(2026, 10, 19, 13, 20, tzinfo=UTC))
    assert tw.in_window(datetime(2026, 10, 19, 20, 10, tzinfo=UTC))  # 16:10 et
    assert not tw.in_window(datetime(2026, 10, 19, 20, 16, tzinfo=UTC))
    assert not tw.in_window(datetime(2026, 10, 17, 15, 0, tzinfo=UTC))  # saturday


def test_read_only_client_has_no_write_method() -> None:
    assert not any(name in dir(tw.ReadOnlyHTTP) for name in ("post", "put", "patch", "delete"))
    assert not any(name in dir(tw.RobinhoodClient) for name in ("place_order", "cancel", "post"))
