"""Event gate: ask HVega's event worker about a card before it goes to the phone.

The worker (HVega ``hvega events serve``) keeps calendars (FOMC, CPI, Copom, elections),
prediction-market moves and matched news headlines. Its ``POST /gate`` marks a card
regime=event when its product or driver had an event in the prior 6h, suppresses
mean-reversion fades, and puts the events in the body. It also range-checks the driver's
quote convention: 6L and BRL-USD are dollars per real, so a "6L" above 1 is an inverted feed.

Why: on 2026-10-26 a 6L move fired two EWZ fade cards (01:39 and 02:44 UTC) an hour after
Lula's upset re-election, with no event context.

``EVENT_GATE_URL`` turns it on (unset = off, the card goes out unchanged). A gate that
cannot be reached never blocks a card: it goes out with one line saying the gate was down.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

import requests

log = logging.getLogger("event_gate")

FADE = "mean_reversion_fade"
_HOPING = re.compile(r"^Hoping For:(.*)$", re.MULTILINE | re.IGNORECASE)
_TICKER = re.compile(r"^ticker:\s*(\S+)", re.MULTILINE)

Post = Callable[[str, dict[str, Any], float], list[dict[str, Any]]]


def _post(url: str, payload: dict[str, Any], timeout: float) -> list[dict[str, Any]]:
    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    out: list[dict[str, Any]] = r.json()
    return out


def card_label(body: str) -> str:
    """mean_reversion_fade when the card hopes for a fade or a mean reversion, else 'card'."""
    m = _HOPING.search(body)
    hoping = (m.group(1) if m else "").lower()
    return FADE if ("mean reversion" in hoping or "fade" in hoping) else "card"


def card_ticker(body: str, default: str) -> str:
    m = _TICKER.search(body)
    return m.group(1) if m else default


class EventGate:
    def __init__(self, url: str | None, timeout: float = 4.0, post: Post = _post) -> None:
        self.url, self.timeout, self.post = url, timeout, post

    @classmethod
    def from_env(cls) -> EventGate:
        return cls(os.environ.get("EVENT_GATE_URL") or None)

    def apply(
        self,
        title: str,
        body: str,
        now: datetime,
        product: str,
        driver: str | None = None,
        move_pct: float | None = None,
        price: float | None = None,
    ) -> tuple[str, str]:
        """(title, body) to push. Unchanged when the gate is off; never raises."""
        if not self.url:
            return title, body
        alert: dict[str, Any] = {
            "ts": now.isoformat(),
            "ticker": card_ticker(body, product),
            "label": card_label(body),
            "title": title,
            "body": body,
            "driver": driver,
            "driver_move": None if move_pct is None else move_pct / 100.0,
            "driver_price": price,
        }
        try:
            g = self.post(self.url, {"alerts": [alert]}, self.timeout)[0]
        except Exception as exc:  # the gate is advice: a dead gate never blocks a card
            log.warning("event gate unreachable: %s", exc)
            return (
                title,
                f"{body}\nevent gate: unreachable ({type(exc).__name__}); no event context",
            )
        out_title, out_body = str(g["title"]), str(g["body"])
        if g.get("hold"):
            out_body = f"DO NOT TRADE: {g['hold']}\n\n{out_body}"
        elif g.get("suppressed"):
            out_body = f"DO NOT OPEN: fade suppressed, event in the last 6h\n\n{out_body}"
        return out_title, out_body
