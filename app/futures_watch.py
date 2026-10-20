"""Futures watcher: ES, NQ and VIX futures front and back month, gold, crude, GLD.

Runs as a thread inside the vol-watch service (started by ``app.vol_watch.main``) on
futures hours, Sunday 18:00 ET through Friday 17:00 ET, ticking every
``FUTURES_TICK_MINUTES`` (5). It pushes:

* a snapshot at each time in ``FUTURES_SNAPSHOT_TIMES`` (06:30, 08:30, 12:30, 16:30 ET):
  every contract's last, change versus the prior settle and the trailing-hour move,
  the VIX futures spread with contango or backwardation, and VIX against VIX3M;
* a ``MOVE`` alert when a contract moves more than its threshold inside the trailing
  hour (ES and NQ 0.75%, GC 1%, CL 2%, VX 8%), one per contract per hour;
* an ``OVERNIGHT`` alert when ES is more than 1% from the prior settle outside the
  cash session, once per session day;
* a ``VIX TERM`` alert when the front month crosses above or below the back month
  (backwardation on, backwardation off).

Equity index, metal and energy contracts come from Yahoo (``ESZ26.CME`` style symbols,
chosen by the roll rules below and verified to return bars). VIX futures come from
Cboe's delayed combined quotes, monthly contracts only (``VX/V6``), with the prior
settlement in the same payload. Everything else about the contract choice is written
to the state file each day so a reader can see which symbols the numbers came from.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("futures_watch")

ET = ZoneInfo("America/New_York")
MONTH_CODES = "FGHJKMNQUVXZ"  # Jan..Dec
QUARTERLY = "HMUZ"
GOLD_MONTHS = "GJMQVZ"
CBOE_VX_URL = "https://www.cboe.com/us/futures/api/get_quotes_combined/?symbol=VX&rootsymbol=VX"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)

DEFAULT_MOVE_PCT = {
    "ES": 0.75,
    "NQ": 0.75,
    "GC": 1.0,
    "CL": 2.0,
    "VX": 8.0,
    "GLD": 1.0,
    "ZS": 1.5,
    "SB": 2.5,
    "HG": 1.5,
    "6L": 0.75,
    "BRL": 0.75,
    "ARS": 1.5,
    "ETH": 3.0,
    "SOL": 4.0,
}

# Front-month continuous contracts (Yahoo rolls these) for the commodity and currency
# futures, then spot FX and crypto. Label -> Yahoo symbol; the root is the first word.
CONTINUOUS = {
    "ZS front": "ZS=F",  # soybeans
    "SB front": "SB=F",  # sugar no. 11
    "HG front": "HG=F",  # copper
    "6L front": "6L=F",  # brazilian real future
}
FX = {
    "BRL-USD": "BRLUSD=X",  # dollars per real
    "USD-ARS": "ARS=X",  # pesos per dollar, the official managed rate
}
CRYPTO = {"ETH-USD": "ETH-USD", "SOL-USD": "SOL-USD"}
ROOT_OF = {"BRL-USD": "BRL", "USD-ARS": "ARS", "ETH-USD": "ETH", "SOL-USD": "SOL"}


@dataclass(frozen=True)
class Config:
    tick_minutes: int = 5
    snapshot_times: tuple[str, ...] = ("06:30", "08:30", "12:30", "16:30")
    move_pct: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_MOVE_PCT))
    overnight_pct: float = 1.0
    cooldown_minutes: int = 60
    window_minutes: int = 60
    state_path: Path = Path("futures_watch_state.json")
    es_levels: tuple[float, ...] = (7700.0, 7720.0, 7740.0, 7800.0, 7840.0, 7880.0)
    level_cooldown_minutes: int = 30

    @classmethod
    def from_env(cls) -> Config:
        raw_levels = os.environ.get("FUTURES_ES_LEVELS") or ""
        levels: list[float] = []
        for part in raw_levels.split(","):
            with contextlib.suppress(ValueError):
                levels.append(float(part.strip()))
        raw_times = os.environ.get("FUTURES_SNAPSHOT_TIMES") or "06:30,08:30,12:30,16:30"
        times = tuple(t.strip() for t in raw_times.split(",") if t.strip())
        move = dict(DEFAULT_MOVE_PCT)
        for root in move:
            raw = os.environ.get(f"FUTURES_MOVE_{root}")
            if raw:
                with contextlib.suppress(ValueError):
                    move[root] = float(raw)
        data_dir = Path("/data") if Path("/data").is_dir() else Path(".")
        return cls(
            tick_minutes=max(1, int(os.environ.get("FUTURES_TICK_MINUTES") or 5)),
            snapshot_times=times,
            move_pct=move,
            overnight_pct=float(os.environ.get("FUTURES_OVERNIGHT_PCT") or 1.0),
            cooldown_minutes=int(os.environ.get("FUTURES_COOLDOWN_MINUTES") or 60),
            window_minutes=int(os.environ.get("FUTURES_WINDOW_MINUTES") or 60),
            state_path=Path(
                os.environ.get("FUTURES_WATCH_STATE_PATH") or data_dir / "futures_watch_state.json"
            ),
            es_levels=tuple(sorted(levels)) if levels else cls.es_levels,
            level_cooldown_minutes=int(os.environ.get("FUTURES_LEVEL_COOLDOWN_MINUTES") or 30),
        )


# ------------------------------------------------------------------- contracts


def third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    offset = (4 - first.weekday()) % 7  # Friday = 4
    return first + timedelta(days=offset + 14)


def _month_iter(start: date) -> list[tuple[int, int]]:
    out = []
    y, m = start.year, start.month
    for _ in range(18):
        out.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def contract_months(root: str, today: date) -> list[tuple[int, int]]:
    """Front and back contract (year, month) for a root under its roll rule.

    ES/NQ: quarterly, roll a week before the third Friday. GC: even-month actives,
    front once the contract month starts more than ten days out. CL: monthly, expires
    around the 20th of the prior month, roll three days before that.
    """
    picks: list[tuple[int, int]] = []
    for y, m in _month_iter(today):
        if root in ("ES", "NQ"):
            if MONTH_CODES[m - 1] not in QUARTERLY:
                continue
            if third_friday(y, m) - timedelta(days=7) <= today:
                continue
        elif root == "GC":
            if MONTH_CODES[m - 1] not in GOLD_MONTHS:
                continue
            if (date(y, m, 1) - today).days <= 10:
                continue
        elif root == "CL":
            prev_y, prev_m = (y, m - 1) if m > 1 else (y - 1, 12)
            if (date(prev_y, prev_m, 20) - today).days <= 3:
                continue
        else:
            continue
        picks.append((y, m))
        if len(picks) == 2:
            break
    return picks


EXCHANGE = {"ES": "CME", "NQ": "CME", "GC": "CMX", "CL": "NYM"}


def yahoo_symbol(root: str, year: int, month: int) -> str:
    return f"{root}{MONTH_CODES[month - 1]}{year % 100:02d}.{EXCHANGE[root]}"


def contract_label(root: str, year: int, month: int) -> str:
    return f"{root} {MONTH_CODES[month - 1]}{year % 100 % 10}"


# ------------------------------------------------------------------------ data


@dataclass
class Quote:
    label: str  # "ES Z6", "VX V6", "GLD"
    root: str
    last: float
    prev_settle: float | None
    hour_ago: float | None  # price about one hour ago (window reference)
    hour_high: float | None
    hour_low: float | None
    as_of: str

    @property
    def change_pct(self) -> float | None:
        if not self.prev_settle:
            return None
        return (self.last / self.prev_settle - 1.0) * 100.0

    @property
    def hour_move_pct(self) -> float | None:
        """Worst-case trailing-hour move: from the hour high if down, hour low if up."""
        if self.hour_high is None or self.hour_low is None or self.hour_ago is None:
            return None
        down = (self.last / self.hour_high - 1.0) * 100.0
        up = (self.last / self.hour_low - 1.0) * 100.0
        return down if abs(down) >= abs(up) else up


class Source(Protocol):
    def yahoo_quotes(self, symbols: dict[str, str], window_minutes: int) -> dict[str, Quote]: ...

    def vx_quotes(self) -> list[dict[str, Any]]: ...


class LiveSource:
    def __init__(self) -> None:
        import yfinance as yf

        self._yf = yf
        self.session = requests.Session()

    def yahoo_quotes(self, symbols: dict[str, str], window_minutes: int) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for label, symbol in symbols.items():
            try:
                tk = self._yf.Ticker(symbol)
                bars = tk.history(period="2d", interval="5m")["Close"].dropna()
                daily = tk.history(period="7d", interval="1d")["Close"].dropna()
            except Exception as exc:
                log.warning("%s (%s) failed: %s", label, symbol, exc)
                continue
            if bars.empty:
                continue
            end = bars.index[-1]
            window = bars[bars.index >= end - timedelta(minutes=window_minutes)]
            today_et = end.tz_convert(ET).date()
            if symbol.endswith("=X") or symbol.endswith("-USD"):
                # fx and crypto have no settle and their daily bars roll at utc midnight,
                # so "vs settle" is measured against the price 24 hours ago instead
                day_ago = bars[bars.index <= end - timedelta(hours=24)]
                ref = float(day_ago.iloc[-1]) if not day_ago.empty else None
                out[label] = Quote(
                    label=label,
                    root=ROOT_OF.get(label, label.split(" ")[0]),
                    last=float(bars.iloc[-1]),
                    prev_settle=ref,
                    hour_ago=float(window.iloc[0]) if len(window) > 1 else None,
                    hour_high=float(window.max()) if len(window) > 1 else None,
                    hour_low=float(window.min()) if len(window) > 1 else None,
                    as_of=end.isoformat(),
                )
                continue
            prior = [
                v
                for d, v in zip(daily.index, daily, strict=False)
                if d.tz_convert(ET).date() < today_et
            ]
            out[label] = Quote(
                label=label,
                root=ROOT_OF.get(label, label.split(" ")[0]),
                last=float(bars.iloc[-1]),
                prev_settle=float(prior[-1]) if prior else None,
                hour_ago=float(window.iloc[0]) if len(window) > 1 else None,
                hour_high=float(window.max()) if len(window) > 1 else None,
                hour_low=float(window.min()) if len(window) > 1 else None,
                as_of=end.isoformat(),
            )
        return out

    def vx_quotes(self) -> list[dict[str, Any]]:
        response = self.session.get(
            CBOE_VX_URL,
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
            timeout=20,
        )
        response.raise_for_status()
        return list(response.json().get("data") or [])


def monthly_vx(rows: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Monthly VX contracts (``VX/V6``, not the weekly ``VX43/V6``) expiring after today."""
    out = []
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if not (symbol.startswith("VX/") and len(symbol) == 5):
            continue
        try:
            exp = datetime.strptime(str(row.get("expiration")), "%m/%d/%Y").date()
        except ValueError:
            continue
        if exp <= today:
            continue
        last = float(row.get("last_price") or 0.0)
        if last <= 0:
            continue
        out.append({**row, "_exp": exp, "_label": f"VX {symbol[3:]}"})
    out.sort(key=lambda r: r["_exp"])
    return out


# ------------------------------------------------------------------------ state


@dataclass
class State:
    day: str = ""
    contracts: dict[str, str] = field(default_factory=dict)  # label -> yahoo symbol
    last_alert: dict[str, str] = field(default_factory=dict)
    snapshots_sent: list[str] = field(default_factory=list)  # "2026-10-19 08:30"
    overnight_sent: str = ""
    backwardation: bool | None = None
    vx_history: dict[str, list[list[Any]]] = field(default_factory=dict)  # label -> [[iso, px]]
    es_side: dict[str, str] = field(default_factory=dict)  # "7740" -> above | below

    @classmethod
    def load(cls, path: Path) -> State:
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            return cls()
        state = cls()
        for key in ("day", "overnight_sent"):
            setattr(state, key, str(raw.get(key) or ""))
        state.contracts = dict(raw.get("contracts") or {})
        state.last_alert = dict(raw.get("last_alert") or {})
        state.snapshots_sent = list(raw.get("snapshots_sent") or [])
        state.backwardation = raw.get("backwardation")
        state.vx_history = {k: list(v) for k, v in (raw.get("vx_history") or {}).items()}
        state.es_side = dict(raw.get("es_side") or {})
        return state

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=1, sort_keys=True, default=str))
        tmp.replace(path)


# ------------------------------------------------------------------- rendering


def pct(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:+.{digits}f}%"


def px(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if digits == 2 and abs(value) < 1:
        digits = 4  # BRL-USD and the real future quote in fractions of a dollar
    return f"{value:,.{digits}f}"


def render_snapshot(quotes: dict[str, Quote], when: str) -> tuple[str, str]:
    lines: list[str] = []
    for root in ("ES", "NQ"):
        pair = [q for q in quotes.values() if q.root == root]
        if pair:
            front = pair[0]
            text = (
                f"{front.label} {px(front.last)} "
                f"({pct(front.change_pct)} vs settle, {pct(front.hour_move_pct)} 1h)"
            )
            if len(pair) > 1:
                text += f" | {pair[1].label} {px(pair[1].last)}"
            lines.append(text)
    vx = [q for q in quotes.values() if q.root == "VX"]
    if vx:
        front = vx[0]
        text = f"{front.label} {px(front.last)} ({pct(front.change_pct, 1)} vs settle)"
        if len(vx) > 1:
            spread = vx[1].last - front.last
            shape = "contango" if spread > 0 else "backwardation"
            text += f" | {vx[1].label} {px(vx[1].last)} | spread {spread:+.2f} {shape}"
        spot, three = quotes.get("VIX"), quotes.get("VIX3M")
        if spot:
            text += f" | VIX {px(spot.last)}"
            if three:
                text += f" VIX3M {px(three.last)}"
        lines.append(text)
    gc = [q for q in quotes.values() if q.root == "GC"]
    if gc:
        text = f"{gc[0].label} {px(gc[0].last, 1)} ({pct(gc[0].change_pct)})"
        if len(gc) > 1:
            text += f" | {gc[1].label} {px(gc[1].last, 1)}"
        gld = quotes.get("GLD")
        if gld:
            text += f" | GLD {px(gld.last)} ({pct(gld.change_pct)})"
        lines.append(text)
    cl = [q for q in quotes.values() if q.root == "CL"]
    if cl:
        text = f"{cl[0].label} {px(cl[0].last)} ({pct(cl[0].change_pct)})"
        if len(cl) > 1:
            text += f" | {cl[1].label} {px(cl[1].last)}"
        lines.append(text)
    ags = [
        f"{q.root} {px(q.last)} ({pct(q.change_pct)})"
        for q in quotes.values()
        if q.label in CONTINUOUS
    ]
    if ags:
        lines.append(" | ".join(ags))
    fx = [
        f"{q.label} {px(q.last, 4 if q.root == 'BRL' else 2)} ({pct(q.change_pct)})"
        for q in quotes.values()
        if q.label in FX
    ]
    if fx:
        lines.append(" | ".join(fx) + " (24h change; ARS is the official managed rate)")
    crypto = [
        f"{q.root} {px(q.last)} ({pct(q.change_pct)})" for q in quotes.values() if q.label in CRYPTO
    ]
    if crypto:
        lines.append(" | ".join(crypto) + " (24h change)")
    return f"futures {when}", "\n".join(lines) if lines else "no futures quotes"


# ----------------------------------------------------------------------- watcher


class Pusher(Protocol):
    def push(
        self,
        title: str,
        body: str,
        priority: str = "default",
        payload: dict[str, Any] | None = None,
    ) -> None: ...


def futures_open(now: datetime) -> bool:
    local = now.astimezone(ET)
    wd, t = local.weekday(), local.time()
    if wd == 5:
        return False
    if wd == 6:
        return t >= dtime(18, 0)
    if wd == 4:
        return t < dtime(17, 0)
    return not (dtime(17, 0) <= t < dtime(18, 0))


def cash_session(now: datetime) -> bool:
    local = now.astimezone(ET)
    return local.weekday() < 5 and dtime(9, 30) <= local.time() < dtime(16, 0)


class Watcher:
    def __init__(self, cfg: Config, source: Source, pusher: Pusher, cards: Any = None) -> None:
        self.cfg = cfg
        self.source = source
        self.pusher = pusher
        self.cards = cards
        self.state = State.load(cfg.state_path)

    def _facts(self, quotes: dict[str, Quote]) -> dict[str, Any]:
        vx = [q for q in quotes.values() if q.root == "VX"]
        es = next((q for q in quotes.values() if q.root == "ES"), None)
        vix = quotes.get("VIX")
        return {
            "vix": vix.last if vix else None,
            "vx_front": vx[0].last if vx else None,
            "vx_back": vx[1].last if len(vx) > 1 else None,
            "vx_hour_move": vx[0].hour_move_pct if vx else None,
            "es_change": es.change_pct if es else None,
        }

    def _card(self, method: str, *args: Any) -> tuple[str, str] | None:
        if self.cards is None:
            return None
        try:
            result: tuple[str, str] = getattr(self.cards, method)(*args)
            return result
        except Exception as exc:
            log.warning("card render failed (%s): %s", method, exc)
            return None

    def _contracts(self, today: date) -> dict[str, str]:
        if self.state.day == today.isoformat() and self.state.contracts:
            return self.state.contracts
        symbols: dict[str, str] = {}
        for root in ("ES", "NQ", "GC", "CL"):
            for y, m in contract_months(root, today):
                symbols[contract_label(root, y, m)] = yahoo_symbol(root, y, m)
        symbols["GLD"] = "GLD"
        symbols["VIX"] = "^VIX"
        symbols["VIX3M"] = "^VIX3M"
        symbols.update(CONTINUOUS)
        symbols.update(FX)
        symbols.update(CRYPTO)
        self.state.day = today.isoformat()
        self.state.contracts = symbols
        self.state.snapshots_sent = [
            s for s in self.state.snapshots_sent if s.startswith(today.isoformat())
        ]
        return symbols

    def _vx(self, now: datetime, today: date) -> dict[str, Quote]:
        try:
            rows = monthly_vx(self.source.vx_quotes(), today)
        except Exception as exc:
            log.warning("cboe vx quotes failed: %s", exc)
            return {}
        out: dict[str, Quote] = {}
        cutoff = now - timedelta(minutes=self.cfg.window_minutes)
        for row in rows[:2]:
            label = row["_label"]
            hist = [
                h
                for h in self.state.vx_history.get(label, [])
                if datetime.fromisoformat(h[0]) >= cutoff
            ]
            hist.append([now.isoformat(), float(row["last_price"])])
            self.state.vx_history[label] = hist
            prices = [float(h[1]) for h in hist]
            out[label] = Quote(
                label=label,
                root="VX",
                last=float(row["last_price"]),
                prev_settle=float(row.get("prev_settlement") or 0) or None,
                hour_ago=prices[0] if len(prices) > 1 else None,
                hour_high=max(prices) if len(prices) > 1 else None,
                hour_low=min(prices) if len(prices) > 1 else None,
                as_of=now.isoformat(),
            )
        return out

    def quotes(self, now: datetime, crypto_only: bool = False) -> dict[str, Quote]:
        today = now.astimezone(ET).date()
        symbols = self._contracts(today)
        if crypto_only:
            symbols = {k: v for k, v in symbols.items() if k in CRYPTO}
        quotes = self.source.yahoo_quotes(symbols, self.cfg.window_minutes)
        ordered: dict[str, Quote] = {}
        for label in symbols:
            if label in quotes:
                ordered[label] = quotes[label]
        if not crypto_only:
            ordered.update(self._vx(now, today))
        return ordered

    def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """One pass. Outside futures hours only crypto is read (it trades all weekend)."""
        now = now or datetime.now(UTC)
        local = now.astimezone(ET)
        crypto_only = not futures_open(now)
        quotes = self.quotes(now, crypto_only=crypto_only)
        events: list[dict[str, Any]] = []
        events.extend(self._moves(quotes, now))
        if not crypto_only:
            events.extend(self._levels(quotes, now))
            events.extend(self._term(quotes))
            events.extend(self._overnight(quotes, now))
            events.extend(self._snapshot(quotes, local))
        self.state.save(self.cfg.state_path)
        return events

    def _cooled(self, key: str, now: datetime) -> bool:
        last = self.state.last_alert.get(key)
        if last and now - datetime.fromisoformat(last) < timedelta(
            minutes=self.cfg.cooldown_minutes
        ):
            return False
        self.state.last_alert[key] = now.isoformat()
        return True

    def _levels(self, quotes: dict[str, Quote], now: datetime) -> list[dict[str, Any]]:
        """ES crossing one of his levels, either direction, one push per level per cooldown."""
        es = next((q for q in quotes.values() if q.root == "ES"), None)
        if es is None:
            return []
        events = []
        for level in self.cfg.es_levels:
            key = f"{level:g}"
            side = "above" if es.last >= level else "below"
            prev = self.state.es_side.get(key)
            self.state.es_side[key] = side
            if prev is None or prev == side:
                continue
            last = self.state.last_alert.get(f"level:{key}")
            if last and now - datetime.fromisoformat(last) < timedelta(
                minutes=self.cfg.level_cooldown_minutes
            ):
                continue
            self.state.last_alert[f"level:{key}"] = now.isoformat()
            arrow = "<" if side == "below" else ">"
            title = f"ES {arrow} {level:g}"
            body = (
                f"{es.label} {px(es.last)} crossed {side} {level:g} | "
                f"{pct(es.change_pct)} vs settle"
            )
            card = self._card("es_level", level, side, es.last, self._facts(quotes))
            if card:
                title, body = card
            self.pusher.push(title, body, "high")
            events.append({"kind": "level", "level": level, "side": side})
        return events

    def _moves(self, quotes: dict[str, Quote], now: datetime) -> list[dict[str, Any]]:
        events = []
        seen_roots: set[str] = set()
        for q in quotes.values():
            if q.root in seen_roots or q.root not in self.cfg.move_pct:
                continue
            seen_roots.add(q.root)  # front month only
            move = q.hour_move_pct
            if move is None or abs(move) < self.cfg.move_pct[q.root]:
                continue
            if not self._cooled(f"move:{q.root}", now):
                continue
            ref = q.hour_high if move < 0 else q.hour_low
            title = f"MOVE {q.label} {move:+.2f}% in {self.cfg.window_minutes}m"
            body = f"{q.label} {px(ref)} -> {px(q.last)} | {pct(q.change_pct)} vs settle"
            vx = [v for v in quotes.values() if v.root == "VX"]
            if vx and q.root != "VX":
                body += f" | {vx[0].label} {px(vx[0].last)} ({pct(vx[0].hour_move_pct, 1)} 1h)"
            card = self._card("futures_move", q.label, q.root, move, self._facts(quotes))
            if card:
                title, body = card
            self.pusher.push(title, body, "high" if q.root in ("ES", "NQ", "VX") else "default")
            events.append({"kind": "move", "label": q.label, "move": move})
        return events

    def _term(self, quotes: dict[str, Quote]) -> list[dict[str, Any]]:
        vx = [q for q in quotes.values() if q.root == "VX"]
        if len(vx) < 2:
            return []
        back = vx[0].last > vx[1].last
        prev = self.state.backwardation
        self.state.backwardation = back
        if prev is None or prev == back:
            return []
        spread = vx[1].last - vx[0].last
        title = "VIX TERM: backwardation" if back else "VIX TERM: back to contango"
        body = (
            f"{vx[0].label} {px(vx[0].last)} vs {vx[1].label} {px(vx[1].last)} "
            f"(spread {spread:+.2f})"
        )
        body += (
            ". front over back: stress is now, sell the spike with wings."
            if back
            else ". curve normal again."
        )
        card = self._card("vix_term", back, vx[0].label, vx[1].label, spread, self._facts(quotes))
        if card:
            title, body = card
        self.pusher.push(title, body, "high")
        return [{"kind": "term", "backwardation": back}]

    def _overnight(self, quotes: dict[str, Quote], now: datetime) -> list[dict[str, Any]]:
        if cash_session(now):
            return []
        es = next((q for q in quotes.values() if q.root == "ES"), None)
        if es is None or es.change_pct is None or abs(es.change_pct) < self.cfg.overnight_pct:
            return []
        session_day = (now.astimezone(ET) + timedelta(hours=6)).date().isoformat()  # 18:00 roll
        if self.state.overnight_sent == session_day:
            return []
        self.state.overnight_sent = session_day
        title = f"OVERNIGHT {es.label} {pct(es.change_pct)} vs settle"
        body = f"{es.label} {px(es.prev_settle)} -> {px(es.last)}"
        nq = next((q for q in quotes.values() if q.root == "NQ"), None)
        if nq:
            body += f" | {nq.label} {pct(nq.change_pct)}"
        vx = next((q for q in quotes.values() if q.root == "VX"), None)
        if vx:
            body += f" | {vx.label} {px(vx.last)} ({pct(vx.change_pct, 1)})"
        card = self._card("overnight", es.label, es.change_pct, self._facts(quotes))
        if card:
            title, body = card
        self.pusher.push(title, body, "high")
        return [{"kind": "overnight", "change": es.change_pct}]

    def _snapshot(self, quotes: dict[str, Quote], local: datetime) -> list[dict[str, Any]]:
        events = []
        for slot in self.cfg.snapshot_times:
            hh, mm = (int(x) for x in slot.split(":"))
            due = local.time() >= dtime(hh, mm)
            marker = f"{local.date().isoformat()} {slot}"
            late = (
                local.time()
                >= (datetime.combine(local.date(), dtime(hh, mm)) + timedelta(minutes=90)).time()
            )
            if not due or marker in self.state.snapshots_sent:
                continue
            self.state.snapshots_sent.append(marker)
            if late:  # service was down through the slot: skip rather than send stale
                continue
            title, body = render_snapshot(quotes, f"{slot} ET")
            self.pusher.push(title, body)
            events.append({"kind": "snapshot", "slot": slot})
        return events


def run_forever(cfg: Config, source: Source, pusher: Pusher, cards: Any = None) -> None:
    watcher = Watcher(cfg, source, pusher, cards)
    while True:
        try:
            watcher.tick(datetime.now(UTC))  # crypto-only outside futures hours
        except Exception:
            log.exception("futures tick failed")
        time.sleep(cfg.tick_minutes * 60)


def start_thread(pusher: Pusher, cards: Any = None) -> threading.Thread:
    cfg = Config.from_env()
    thread = threading.Thread(
        target=run_forever,
        args=(cfg, LiveSource(), pusher, cards),
        name="futures-watch",
        daemon=True,
    )
    thread.start()
    log.info(
        "futures watcher started: snapshots %s, es levels %s",
        ",".join(cfg.snapshot_times),
        ",".join(f"{x:g}" for x in cfg.es_levels),
    )
    return thread


def snapshot_now(pusher: Pusher | None = None) -> str:
    """One live snapshot, printed and optionally pushed. Used by --futures-once."""
    cfg = Config.from_env()
    watcher = Watcher(cfg, LiveSource(), pusher or _NullPusher())
    now = datetime.now(UTC)
    quotes = watcher.quotes(now)
    title, body = render_snapshot(quotes, now.astimezone(ET).strftime("%H:%M ET"))
    if pusher is not None:
        pusher.push(title, body)
    return f"{title}\n{body}"


class _NullPusher:
    def push(self, title: str, body: str, priority: str = "default", payload: Any = None) -> None:
        del title, body, priority, payload
