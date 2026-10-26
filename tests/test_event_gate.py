from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import futures_watch as fw
from app import trade_cards as tc
from app.event_gate import FADE, EventGate, card_label, card_ticker

AT = datetime(2026, 10, 26, 1, 39, tzinfo=UTC)
FADE_CARD = (
    "ticker: EWZ\nOpen: sell EWZ 44/45 call spread\nClose: 50%\nEV: n/a\nTimeframe: 1wk\n"
    "Theta: n/a\n"
    "Hoping For: the hour move in 6L front exhausts; mean reversion, spread out of reach\n"
    "Reason: 6L front +1.96% in the last hour\nadditional context: vix 18"
)


def test_card_label_and_ticker() -> None:
    assert card_label(FADE_CARD) == FADE
    assert card_label("ticker: XSP\nHoping For: vix spike fades, ES holds") == FADE
    assert card_label("ticker: XSP\nHoping For: ES follows through to 7720") == "card"
    assert card_ticker(FADE_CARD, "6L") == "EWZ" and card_ticker("no ticker line", "6L") == "6L"


def test_gate_off_by_default_leaves_the_card_alone() -> None:
    assert EventGate(None).apply("t", FADE_CARD, AT, "EWZ", "6L", 1.96, 0.1799) == ("t", FADE_CARD)


def test_unreachable_gate_never_blocks() -> None:
    def down(url: str, payload: dict[str, Any], timeout: float) -> list[dict[str, Any]]:
        del url, payload, timeout
        raise ConnectionError("no route")

    title, body = EventGate("https://gate", post=down).apply(
        "t", FADE_CARD, AT, "EWZ", "6L", 1.96, 0.1799
    )
    assert title == "t" and body.startswith(FADE_CARD) and "event gate: unreachable" in body


def test_suppressed_fade_is_marked_do_not_open_and_payload_is_typed() -> None:
    sent: list[dict[str, Any]] = []

    def gate(url: str, payload: dict[str, Any], timeout: float) -> list[dict[str, Any]]:
        del url, timeout
        sent.append(payload)
        a = payload["alerts"][0]
        return [
            {
                "title": a["title"] + " · regime=event · fade suppressed",
                "suppressed": True,
                "hold": None,
                "body": a["body"]
                + "\n· 10-26 00:31Z rss:wsj_world: Lula Clings to Power in Brazil Election Upset",
            }
        ]

    title, body = EventGate("https://gate", post=gate).apply(
        "MOVE 6L front +1.96%", FADE_CARD, AT, "6L", "6L", 1.96, 0.1799
    )
    a = sent[0]["alerts"][0]
    assert a["ticker"] == "EWZ" and a["driver"] == "6L" and a["label"] == FADE
    assert (
        abs(a["driver_move"] - 0.0196) < 1e-12
        and a["driver_price"] == 0.1799
        and a["ts"].startswith("2026-10-26T01:39")
    )
    assert title.endswith("fade suppressed") and body.startswith("DO NOT OPEN")
    assert "Lula Clings to Power" in body


def test_hold_says_do_not_trade() -> None:
    def gate(url: str, payload: dict[str, Any], timeout: float) -> list[dict[str, Any]]:
        del url, payload, timeout
        return [
            {
                "title": "HOLD t",
                "suppressed": False,
                "body": "b",
                "hold": "6L printed 5.55, which is brl per usd, not usd per brl: feed inverted",
            }
        ]

    _, body = EventGate("https://gate", post=gate).apply("t", FADE_CARD, AT, "6L", "6L", 1.96, 5.55)
    assert body.startswith("DO NOT TRADE: 6L printed 5.55")


class _Source:
    def __init__(self) -> None:
        self.quotes: dict[str, fw.Quote] = {}

    def yahoo_quotes(self, symbols: dict[str, str], window_minutes: int) -> dict[str, fw.Quote]:
        del window_minutes
        return {k: v for k, v in self.quotes.items() if k in symbols}

    def vx_quotes(self) -> list[dict[str, Any]]:
        return []


class _Pusher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def push(self, title: str, body: str, priority: str = "default", payload: Any = None) -> None:
        del payload
        self.sent.append((title, body, priority))


class _Picker:
    def credit_spread(self, *args: Any) -> None:
        del args
        return None


def test_6l_move_card_goes_through_the_gate(tmp_path: Path) -> None:
    """2026-10-26 01:39: a 6L hour move builds an EWZ fade card; the gate sees EWZ, driver 6L."""
    seen: list[dict[str, Any]] = []

    def gate(url: str, payload: dict[str, Any], timeout: float) -> list[dict[str, Any]]:
        del url, timeout
        a = payload["alerts"][0]
        seen.append(a)
        return [
            {
                "title": a["title"] + " · regime=event · fade suppressed",
                "body": a["body"] + "\n· headline",
                "suppressed": True,
                "hold": None,
            }
        ]

    cfg = fw.Config(snapshot_times=(), state_path=tmp_path / "f.json", es_levels=())
    source, pusher = _Source(), _Pusher()
    builder = tc.CardBuilder(_Picker())  # type: ignore[arg-type]
    watcher = fw.Watcher(cfg, source, pusher, builder, EventGate("https://gate", post=gate))
    q = fw.Quote("6L front", "6L", 0.1799, 0.2006, 0.1764, 0.1799, 0.1764, "t")
    events = watcher._moves({"6L front": q}, AT)
    assert len(events) == 1 and len(pusher.sent) == 1
    assert seen[0]["ticker"] == "EWZ" and seen[0]["driver"] == "6L" and seen[0]["label"] == FADE
    assert seen[0]["driver_price"] == 0.1799
    assert pusher.sent[0][0].endswith("fade suppressed") and pusher.sent[0][1].startswith(
        "DO NOT OPEN"
    )
