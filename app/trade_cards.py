"""Trade cards: every alert rendered as one concrete trade in his template.

    ticker:
    Open:
    Close:
    EV:
    Timeframe:
    Theta:
    Hoping For:
    Reason:
    additional context:

The card builder turns a signal (an ES level cross, a fast drop, a vol crossing, a
futures move, a VIX term flip) into a specific structure priced off the live chain:
strikes, mids, deltas and theta are read from quotes that pass the same filter the vol
watcher uses, so nothing on the card is a guess. When the chain is unavailable the card
still goes out with ``Open: n/a (chain unavailable)`` and the reason line intact.

Max loss on any suggested spread is capped at ``CARD_MAX_LOSS`` (300 dollars) so one card
never asks for more than the day's remaining budget would allow twice over.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.vol_watch import (
    MARKET_CLOSE,
    RISK_FREE,
    Quote,
    YahooData,
    bs_theta,
    nearest_delta,
    parity_forward,
    passing_quotes,
)

log = logging.getLogger("trade_cards")

ET = ZoneInfo("America/New_York")

# Index futures map onto the options products he trades.
PRODUCT_FOR = {
    "ES": "XSP",
    "NQ": "QQQ",
    "GC": "GLD",
    "GLD": "GLD",
    "VX": "XSP",
    "HG": "COPX",  # copper -> copper miners
    "ZS": "SOYB",  # soybeans -> soybean fund (thin chain: card may say chain unavailable)
    "SB": "CANE",  # sugar -> sugar fund (thin chain)
    "6L": "EWZ",  # real future -> brazil equities
    "BRL": "EWZ",
}
SPOT_ONLY = {"ETH", "SOL", "ARS"}  # no listed options here: spot or nothing


@dataclass
class Card:
    ticker: str
    open: str
    close: str
    ev: str
    timeframe: str
    theta: str
    hoping_for: str
    reason: str
    context: str

    def render(self) -> str:
        return (
            f"ticker: {self.ticker}\n"
            f"Open: {self.open}\n"
            f"Close: {self.close}\n"
            f"EV: {self.ev}\n"
            f"Timeframe: {self.timeframe}\n"
            f"Theta: {self.theta}\n"
            f"Hoping For: {self.hoping_for}\n"
            f"Reason: {self.reason}\n"
            f"additional context: {self.context}"
        )


@dataclass
class Structure:
    """A priced structure: one or two legs of one type on one expiry."""

    ticker: str
    expiry: str
    days: float
    kind: str
    long: Quote | None
    short: Quote | None
    spot: float

    @property
    def width(self) -> float:
        if self.long is None or self.short is None:
            return 0.0
        return abs(self.long.strike - self.short.strike)

    @property
    def net(self) -> float:
        """Dollars per spread: negative = debit paid, positive = credit received."""
        value = 0.0
        if self.long is not None:
            value -= self.long.mid * 100.0
        if self.short is not None:
            value += self.short.mid * 100.0
        return value

    @property
    def theta_per_day(self) -> float:
        t = self.days / 365.0
        total = 0.0
        if self.long is not None:
            total += bs_theta(self.spot, self.long.strike, t, self.long.iv, self.kind == "call")
        if self.short is not None:
            total -= bs_theta(self.spot, self.short.strike, t, self.short.iv, self.kind == "call")
        return total

    def label(self, qty: int = 1) -> str:
        k = self.kind[0]
        exp = "0dte" if self.days < 1 else self.expiry
        if self.long is not None and self.short is not None:
            if self.net < 0:
                strikes = f"{self.long.strike:g}/{self.short.strike:g}"
                return f"buy {qty}x {strikes} {k} debit spread ({exp})"
            return (
                f"sell {qty}x {self.short.strike:g}/{self.long.strike:g} {k} credit spread ({exp})"
            )
        if self.long is not None:
            return f"buy {qty}x {self.long.strike:g}{k} ({exp})"
        return "n/a"

    def ev_line(self) -> str:
        if self.long is not None and self.short is None:
            d = self.long.delta
            be = (
                self.long.strike + self.long.mid
                if self.kind == "call"
                else self.long.strike - self.long.mid
            )
            p_itm = abs(d) * 100
            return (
                f"debit ${-self.net:.0f}, breakeven {be:.2f}, delta {d:+.2f} ≈ p(itm) {p_itm:.0f}%"
            )
        if self.long is None or self.short is None:
            return "n/a"
        w = self.width * 100.0
        if self.net > 0:
            credit = self.net
            p_itm = abs(self.short.delta)
            ev = credit * (1 - p_itm) - (w - credit) * p_itm
            return (
                f"credit ${credit:.0f}, max loss ${w - credit:.0f}, p(itm) {p_itm * 100:.0f}%, "
                f"ev {ev:+.0f}"
            )
        debit = -self.net
        need = debit / w * 100.0 if w else 0.0
        return (
            f"debit ${debit:.0f}, width ${w:.0f}, needs p(win) > {need:.0f}%; "
            f"long delta {abs(self.long.delta) * 100:.0f}%"
        )


CARD_MAX_LOSS = float(os.environ.get("CARD_MAX_LOSS") or 300.0)
CARD_SPOT_SIZE = float(os.environ.get("CARD_SPOT_SIZE") or 100.0)


class ChainPicker:
    """Reads the chain through the vol watcher's data layer and prices structures."""

    def __init__(self, data: YahooData | None = None) -> None:
        self.data = data or YahooData()
        self._cache: dict[tuple[str, str], tuple[datetime, dict[str, Any], float]] = {}

    def _legs(self, ticker: str, expiry: str, days: float) -> tuple[dict[str, Any], float]:
        key = (ticker, expiry)
        now = datetime.now(UTC)
        hit = self._cache.get(key)
        if hit and now - hit[0] < timedelta(minutes=3):
            return hit[1], hit[2]
        closes = self.data.daily_closes(ticker, period="5d")
        spot = float(closes.iloc[-1])
        legs = self.data._expiry_chain(ticker, expiry, spot, days)
        self._cache[key] = (now, legs, spot)
        return legs, spot

    def expiry(self, ticker: str, target_days: float) -> tuple[str, float]:
        """Listed expiry nearest the target: today for 0, else the first at or past target."""
        today = datetime.now(ET).date()
        exps = []
        for exp in self.data._yf.Ticker(self.data.symbol(ticker)).options:
            try:
                d = (date.fromisoformat(exp) - today).days
            except ValueError:
                continue
            exps.append((d, exp))
        exps.sort()
        if not exps:
            raise RuntimeError(f"{ticker}: no expiries")
        if target_days <= 0 and exps[0][0] == 0:
            close_at = datetime.combine(today, MARKET_CLOSE, tzinfo=ET)
            hours = max((close_at - datetime.now(ET)).total_seconds() / 3600.0, 0.25)
            return exps[0][1], hours / 24.0
        for d, exp in exps:
            if d >= max(target_days, 1):
                return exp, float(d)
        return exps[-1][1], float(exps[-1][0])

    def all_quotes(
        self, ticker: str, expiry: str, days: float, kind: str
    ) -> tuple[list[Quote], float]:
        spot_closes = self.data.daily_closes(ticker, period="5d")
        spot = float(spot_closes.iloc[-1])
        chain = self.data._yf.Ticker(self.data.symbol(ticker)).option_chain(expiry)
        t = max(days, 0.02) / 365.0
        forward = parity_forward(chain.calls, chain.puts, t, self.data.max_rel_spread)
        underlying = forward * math.exp(-RISK_FREE * t) if forward else spot
        frame = chain.calls if kind == "call" else chain.puts
        return passing_quotes(frame, underlying, t, kind == "call", self.data.max_rel_spread), spot

    def long_option(
        self, ticker: str, kind: str, target_days: float, delta: float = 0.45
    ) -> Structure:
        expiry, days = self.expiry(ticker, target_days)
        quotes, spot = self.all_quotes(ticker, expiry, days, kind)
        want = delta if kind == "call" else -delta
        leg = nearest_delta(quotes, want, tolerance=0.2)
        if leg is None:
            raise RuntimeError(f"{ticker}: no {kind} near {delta:.2f} delta passed the filter")
        return Structure(ticker, expiry, days, kind, leg, None, spot)

    def credit_spread(
        self, ticker: str, kind: str, target_days: float, short_delta: float = 0.25
    ) -> Structure:
        expiry, days = self.expiry(ticker, target_days)
        quotes, spot = self.all_quotes(ticker, expiry, days, kind)
        want = short_delta if kind == "call" else -short_delta
        short = nearest_delta(quotes, want, tolerance=0.12)
        if short is None:
            raise RuntimeError(
                f"{ticker}: no {kind} near {short_delta:.2f} delta passed the filter"
            )
        further = _beyond(quotes, short.strike, kind)
        best: Quote | None = None
        for q in sorted(further, key=lambda q: abs(q.strike - short.strike)):
            credit = (short.mid - q.mid) * 100.0
            max_loss = abs(q.strike - short.strike) * 100.0 - credit
            if credit <= 0:
                continue
            if max_loss <= CARD_MAX_LOSS:
                best = q  # widest wing that still fits the cap
            else:
                break
        if best is None:
            raise RuntimeError(f"{ticker}: no wing keeps max loss under ${CARD_MAX_LOSS:.0f}")
        return Structure(ticker, expiry, days, kind, best, short, spot)

    def debit_spread(self, ticker: str, kind: str, target_days: float) -> Structure:
        expiry, days = self.expiry(ticker, target_days)
        quotes, spot = self.all_quotes(ticker, expiry, days, kind)
        long = nearest_delta(quotes, 0.5 if kind == "call" else -0.5, tolerance=0.15)
        if long is None:
            raise RuntimeError(f"{ticker}: no atm {kind} passed the filter")
        further = _beyond(quotes, long.strike, kind)
        target = nearest_delta(further, 0.25 if kind == "call" else -0.25, tolerance=0.15)
        if target is None:
            raise RuntimeError(f"{ticker}: no 25-delta wing for the debit spread")
        return Structure(ticker, expiry, days, kind, long, target, spot)


def _beyond(quotes: list[Quote], strike: float, kind: str) -> list[Quote]:
    """Strikes further out of the money than ``strike`` for the option type."""
    if kind == "call":
        return [q for q in quotes if q.strike > strike]
    return [q for q in quotes if q.strike < strike]


def _kernel_context() -> str:
    """Today's kernel from the trade watcher state file, when it exists."""
    for path in (
        os.environ.get("TRADE_WATCH_STATE_PATH"),
        "/data/trade_watch_state.json",
        "trade_watch_state.json",
    ):
        if not path:
            continue
        try:
            raw = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            continue
        if raw.get("day") != datetime.now(ET).date().isoformat():
            return ""
        realized = float(raw.get("realized") or 0.0)
        structures = raw.get("structures") or {}
        open_loss = sum(
            float(s.get("max_loss", 0)) * (float(s.get("remaining", 0)) / float(s.get("qty") or 1))
            for s in structures.values()
        )
        kernel = max(0.0, -realized) + open_loss
        return f"realized {realized:+.0f}, kernel ${kernel:.0f}/$500"
    return ""


def theta_line(struct: Structure | None) -> str:
    if struct is None:
        return "n/a"
    if struct.days < 1:
        # a same-day contract decays to intrinsic by the close: premium over hours left
        hours = max(struct.days * 24.0, 0.25)
        return f"{struct.net / hours:+.2f} $/hr (0dte, {hours:.1f}h left)"
    return f"{struct.theta_per_day:+.2f} $/day"


class CardBuilder:
    def __init__(self, picker: ChainPicker | None = None) -> None:
        self.picker = picker or ChainPicker()

    def _try(self, fn: Any, *args: Any) -> Structure | None:
        try:
            return fn(*args)
        except Exception as exc:
            log.warning("card chain lookup failed: %s", exc)
            return None

    # ---- futures-driven cards -------------------------------------------------
    def es_level(
        self, level: float, direction: str, es_last: float, facts: dict[str, Any]
    ) -> tuple[str, str]:
        """ES crossed a level. Down with VX calm = gamma phase; up or VX spiked = sell premium."""
        product = "XSP"
        vx_move = facts.get("vx_hour_move")
        spiked = vx_move is not None and vx_move >= 8.0
        ctx = _ctx(facts)
        if direction == "below" and not spiked:
            s = self._try(self.picker.long_option, product, "put", 0, 0.45)
            card = Card(
                ticker=product,
                open=s.label() if s else "n/a (chain unavailable)",
                close=(
                    "sell the next strike down against it once ES is 20 lower (zero-cost spread); "
                    "out by 15:30 either way"
                ),
                ev=s.ev_line() if s else "n/a",
                timeframe="today (1hr to convert, close by 15:30)",
                theta=theta_line(s),
                hoping_for=f"ES follows through {level:.0f} to {level - 20:.0f} before vix wakes",
                reason=(
                    f"ES crossed below {level:.0f} ({es_last:,.2f}); "
                    f"VX front {_pct(vx_move)} 1h, not spiked"
                ),
                context=ctx,
            )
            return f"ES < {level:.0f}", card.render()
        if direction == "below":
            s = self._try(self.picker.credit_spread, product, "put", 0, 0.25)
            card = Card(
                ticker=product,
                open=s.label() if s else "n/a (chain unavailable)",
                close="buy back at 50% of credit or let it expire; stop if the short strike trades",
                ev=s.ev_line() if s else "n/a",
                timeframe="today",
                theta=theta_line(s),
                hoping_for="vix spike fades, ES holds above the short strike into the close",
                reason=(
                    f"ES crossed below {level:.0f} ({es_last:,.2f}) with VX front "
                    f"{_pct(vx_move)} 1h: gamma is priced"
                ),
                context=ctx,
            )
            return f"ES < {level:.0f}", card.render()
        s = self._try(self.picker.credit_spread, product, "call", 0, 0.25)
        card = Card(
            ticker=product,
            open=s.label() if s else "n/a (chain unavailable)",
            close="buy back at 50% of credit; stop if the short strike trades",
            ev=s.ev_line() if s else "n/a",
            timeframe="today",
            theta=theta_line(s),
            hoping_for=f"ES stalls under {level + 40:.0f}; mean reversion off the level",
            reason=f"ES crossed above {level:.0f} ({es_last:,.2f})",
            context=ctx,
        )
        return f"ES > {level:.0f}", card.render()

    def fast_drop(self, proxy: str, hit: dict[str, Any], facts: dict[str, Any]) -> tuple[str, str]:
        product = PRODUCT_FOR.get("ES" if proxy == "SPY" else "NQ", "XSP")
        s = self._try(self.picker.credit_spread, product, "put", 0, 0.25)
        card = Card(
            ticker=product,
            open=s.label() if s else "n/a (chain unavailable)",
            close=(
                "buy back at 50% of credit; if long puts from before, "
                "sell this short strike against them instead"
            ),
            ev=s.ev_line() if s else "n/a",
            timeframe="today",
            theta=theta_line(s),
            hoping_for="put iv spike fades over the next 1-2 hours while ES holds the low",
            reason=(
                f"{proxy} {hit['drop_pct']}% in {hit['window_minutes']}m "
                f"({hit['proxy_high']} -> {hit['proxy_last']}), vix +{hit['vix_jump_pct']}%"
            ),
            context=_ctx(facts),
        )
        return f"FAST DROP {proxy} {hit['drop_pct']}%", card.render()

    def futures_move(
        self, label: str, root: str, move: float, facts: dict[str, Any]
    ) -> tuple[str, str]:
        if root in SPOT_ONLY:
            return self._spot_move(label, root, move, facts)
        product = PRODUCT_FOR.get(root)
        ctx = _ctx(facts)
        if product is None:
            card = Card(
                root,
                "none (no product on the list for this)",
                "n/a",
                "n/a",
                "n/a",
                "n/a",
                "context only",
                f"{label} {move:+.2f}% in the last hour",
                ctx,
            )
            return f"MOVE {label} {move:+.2f}%", card.render()
        kind = "put" if move < 0 else "call"
        s = self._try(self.picker.credit_spread, product, kind, 0 if product == "XSP" else 7, 0.25)
        card = Card(
            ticker=product,
            open=s.label() if s else "n/a (chain unavailable)",
            close="buy back at 50% of credit; stop if the short strike trades",
            ev=s.ev_line() if s else "n/a",
            timeframe="today" if product == "XSP" else "1wk",
            theta=theta_line(s),
            hoping_for=f"the hour move in {label} exhausts; mean reversion, spread out of reach",
            reason=f"{label} {move:+.2f}% in the last hour",
            context=ctx,
        )
        return f"MOVE {label} {move:+.2f}%", card.render()

    def _spot_move(
        self, label: str, root: str, move: float, facts: dict[str, Any]
    ) -> tuple[str, str]:
        """Crypto and the peso have no option chain on the list: spot-only cards."""
        if root == "ARS":
            card = Card(
                ticker=label,
                open="none: no options; the trade, if any, is EWZ or the BRL legs",
                close="n/a",
                ev="n/a",
                timeframe="n/a",
                theta="n/a",
                hoping_for="context only",
                reason=f"{label} {move:+.2f}% in the last hour on the official rate",
                context=(
                    "the official peso rate is managed; the market (blue/mep) rate is a "
                    "different series and can move without this one; " + _ctx(facts)
                ),
            )
            return f"MOVE {label} {move:+.2f}%", card.render()
        size = CARD_SPOT_SIZE
        if move < 0:
            card = Card(
                ticker=label,
                open=f"buy ${size:.0f} {root} spot (max loss ${size:.0f})",
                close=f"sell half on a {abs(move) / 2:.1f}% bounce, rest at the prior hour high",
                ev="n/a: no options, so no implied odds; the size is the max loss",
                timeframe="1hr to 1 day",
                theta="0 (spot)",
                hoping_for=f"mean reversion of a {move:.1f}% hour",
                reason=f"{label} {move:+.2f}% in the last hour",
                context=_ctx(facts),
            )
        else:
            card = Card(
                ticker=label,
                open="none: no defined-risk way to fade a crypto spike here",
                close="if long from lower, sell a third into it",
                ev="n/a",
                timeframe="1hr",
                theta="0 (spot)",
                hoping_for="context only",
                reason=f"{label} {move:+.2f}% in the last hour",
                context=_ctx(facts),
            )
        return f"MOVE {label} {move:+.2f}%", card.render()

    def overnight(self, label: str, change: float, facts: dict[str, Any]) -> tuple[str, str]:
        kind = "put" if change < 0 else "call"
        s = self._try(self.picker.credit_spread, "XSP", kind, 0, 0.25)
        card = Card(
            ticker="XSP",
            open=f"at the open: {s.label()}" if s else "n/a (chain unavailable)",
            close="buy back at 50% of credit; stop if the short strike trades",
            ev=s.ev_line() if s else "n/a",
            timeframe="today",
            theta=theta_line(s),
            hoping_for="gap fades part way; the open prints the extreme of the day",
            reason=f"{label} {change:+.2f}% vs settle overnight",
            context=_ctx(facts),
        )
        return f"OVERNIGHT {label} {change:+.2f}%", card.render()

    def vix_term(
        self, backwardation: bool, front: str, back: str, spread: float, facts: dict[str, Any]
    ) -> tuple[str, str]:
        if backwardation:
            s = self._try(self.picker.credit_spread, "XSP", "put", 7, 0.25)
            card = Card(
                ticker="XSP",
                open=s.label() if s else "n/a (chain unavailable)",
                close="buy back at 50% of credit, or when the curve goes back to contango",
                ev=s.ev_line() if s else "n/a",
                timeframe="1wk",
                theta=theta_line(s),
                hoping_for="stress is priced now: vix front rolls back under the back in days",
                reason=f"VIX term structure flipped: {front} over {back} (spread {spread:+.2f})",
                context=_ctx(facts),
            )
            return "VIX TERM: backwardation", card.render()
        card = Card(
            ticker="XSP",
            open="nothing new",
            close="close any put spreads sold into the backwardation at 50% if not already",
            ev="n/a",
            timeframe="n/a",
            theta="n/a",
            hoping_for="curve normal; back to the daily pair",
            reason=f"VIX term back to contango: {front} under {back} (spread {spread:+.2f})",
            context=_ctx(facts),
        )
        return "VIX TERM: back to contango", card.render()

    # ---- vol-signal cards ---------------------------------------------------
    def vol_signal(self, ticker: str, signal: str, band: str, m: dict[str, Any]) -> tuple[str, str]:
        facts = {"vix": m.get("vix")}
        ctx = _ctx(facts)
        zero = m.get("zero_dte")
        if zero:
            ctx += (
                f"; 0dte straddle {zero.get('implied_move_pct')}% implied "
                f"vs {zero.get('realized_move_pct')}% realized"
            )
        stats = (
            f"iv30 {m.get('iv30')} rv20 {m.get('rv20')} spread {m.get('spread')} "
            f"rank {'n/a' if m.get('iv_rank') is None else m.get('iv_rank')} skew25 {m.get('skew')}"
        )
        days = 0 if ticker == "XSP" else 30
        if signal == "skew" or (signal in ("iv_rank", "spread") and band == "high"):
            s = self._try(self.picker.credit_spread, ticker, "put", days, 0.25)
            why = {
                "skew": f"25d put skew {m.get('skew')} pts: puts bid over calls",
                "iv_rank": f"iv rank {m.get('iv_rank')}: rich against its own year",
                "spread": (
                    f"implied {m.get('iv30')} vs realized {m.get('rv20')}: "
                    "paying for movement the tape isn't delivering"
                ),
            }[signal]
            card = Card(
                ticker=ticker,
                open=s.label() if s else "n/a (chain unavailable)",
                close="buy back at 50% of credit; stop if the short strike trades",
                ev=s.ev_line() if s else "n/a",
                timeframe="today" if days == 0 else "1mo",
                theta=theta_line(s),
                hoping_for=(
                    "volatility crush: implied comes back toward realized "
                    "while price stays above the short strike"
                ),
                reason=f"{why} ({stats})",
                context=ctx,
            )
            title = {
                "skew": f"{ticker} put skew spike",
                "iv_rank": f"{ticker} iv rank rich",
                "spread": f"{ticker} implied >> realized",
            }[signal]
            return title, card.render()
        s = self._try(self.picker.debit_spread, ticker, "call", days)
        p = self._try(self.picker.debit_spread, ticker, "put", days)
        open_text = "n/a (chain unavailable)"
        if s or p:
            open_text = " or ".join(x.label() for x in (s, p) if x) + " on your read"
        card = Card(
            ticker=ticker,
            open=open_text,
            close="sell the further strike once the move pays the debit (zero-cost); else out -50%",
            ev=f"calls: {s.ev_line() if s else 'n/a'} | puts: {p.ev_line() if p else 'n/a'}",
            timeframe="today" if days == 0 else "1mo",
            theta=f"calls {theta_line(s)} | puts {theta_line(p)}",
            hoping_for="a directional move bigger than the cheap implied: gamma is underpriced",
            reason=(
                f"iv rank {m.get('iv_rank')}: cheap against its own year ({stats})"
                if signal == "iv_rank"
                else (
                    f"implied {m.get('iv30')} below realized {m.get('rv20')}: "
                    f"options cheap vs the tape ({stats})"
                )
            ),
            context=ctx,
        )
        title = f"{ticker} iv rank cheap" if signal == "iv_rank" else f"{ticker} implied < realized"
        return title, card.render()


def _pct(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:+.{digits}f}%"


def _ctx(facts: dict[str, Any]) -> str:
    parts = []
    if facts.get("vix") is not None:
        parts.append(f"VIX {facts['vix']:.2f}")
    if facts.get("vx_front") is not None and facts.get("vx_back") is not None:
        spread = facts["vx_back"] - facts["vx_front"]
        shape = "contango" if spread > 0 else "backwardation"
        parts.append(f"VX {facts['vx_front']:.2f}/{facts['vx_back']:.2f} {shape}")
    if facts.get("es_change") is not None:
        parts.append(f"ES {facts['es_change']:+.2f}% vs settle")
    kernel = _kernel_context()
    if kernel:
        parts.append(kernel)
    return "; ".join(parts) if parts else "none"
