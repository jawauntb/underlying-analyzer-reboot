"""Trade watcher: fills, cancels, the day kernel, and a ledger, next to the vol alerts.

Runs inside the vol-watch service (a thread started by ``app.vol_watch.main`` when a
Robinhood token is configured) or on its own (``python -m app.trade_watch``). Between
09:25 and 16:15 ET it polls orders and positions every 30 seconds and:

* pushes ``FILL <side> <qty> <legs> @ <price> | fees <x>`` on every new fill, one push per
  order with all legs and the net debit or credit;
* pushes ``CANCELED <order>`` when a working order is cancelled or rejected;
* keeps the day state: realized P&L (net of fees), open positions with their max loss at
  entry (debit paid, or width minus credit for a spread), and
  ``kernel = realized loss + sum of open max losses``;
* pushes at 50, 80 and 100 percent of the $500 day budget, and ``DAY DOUBLED`` at
  realized >= +$500, each once per day;
* pushes an opening snapshot at 09:31 ET and a close summary at 16:05 ET;
* appends every fill and the close summary to the ``trade-ledger-fills`` Google Sheet
  and to a CSV on the volume.

Read-only by construction: ``RobinhoodClient`` has a ``get`` method and nothing else.
There is no code path in this module that can place, modify or cancel an order. The
only POST it makes is the OAuth token refresh, and only when a refresh token is set.

Every number the state machine produces comes from the broker's own fills; nothing is
estimated. A fill the broker reports without a price is pushed with ``price n/a`` and
is not booked until a later poll carries the price.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("trade_watch")

ET = ZoneInfo("America/New_York")
WINDOW_OPEN = dtime(9, 25)
WINDOW_CLOSE = dtime(16, 15)
SNAPSHOT_AT = dtime(9, 31)
SUMMARY_AT = dtime(16, 5)

DAY_BUDGET = 500.0
KERNEL_STEPS = (0.5, 0.8, 1.0)

LEDGER_COLUMNS = [
    "ts_et",
    "order_id",
    "legs",
    "side",
    "qty",
    "price",
    "net",
    "fees",
    "realized_day",
    "kernel_used",
    "note",
]

RH_BASE = "https://api.robinhood.com"
RH_CLIENT_ID = "c82SH0WZOsabOXGP2sxqcj34FxkvfnWRZBKlBjFS"


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class Config:
    poll_seconds: int = 30
    day_budget: float = DAY_BUDGET
    state_path: Path = Path("trade_watch_state.json")
    ledger_csv: Path = Path("trade_ledger.csv")
    sheet_id: str | None = None
    sheet_title: str = "trade-ledger-fills"
    sheet_share_email: str | None = None
    service_account_json: str | None = None
    rh_token: str | None = None
    rh_refresh_token: str | None = None
    rh_device_token: str | None = None
    ntfy_topic: str | None = None
    ntfy_server: str = "https://ntfy.sh"

    @classmethod
    def from_env(cls) -> Config:
        data_dir = Path("/data") if Path("/data").is_dir() else Path(".")
        return cls(
            poll_seconds=max(5, int(os.environ.get("TRADE_WATCH_POLL_SECONDS") or 30)),
            day_budget=float(os.environ.get("TRADE_WATCH_DAY_BUDGET") or DAY_BUDGET),
            state_path=Path(
                os.environ.get("TRADE_WATCH_STATE_PATH") or data_dir / "trade_watch_state.json"
            ),
            ledger_csv=Path(
                os.environ.get("TRADE_WATCH_LEDGER_CSV") or data_dir / "trade_ledger.csv"
            ),
            sheet_id=os.environ.get("GOOGLE_SHEET_ID") or None,
            sheet_title=os.environ.get("GOOGLE_SHEET_TITLE") or "trade-ledger-fills",
            sheet_share_email=os.environ.get("GOOGLE_SHEET_SHARE_EMAIL") or None,
            service_account_json=os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or None,
            rh_token=os.environ.get("ROBINHOOD_TOKEN") or None,
            rh_refresh_token=os.environ.get("ROBINHOOD_REFRESH_TOKEN") or None,
            rh_device_token=os.environ.get("ROBINHOOD_DEVICE_TOKEN") or None,
            ntfy_topic=os.environ.get("NTFY_TOPIC") or None,
            ntfy_server=(os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/"),
        )

    @property
    def broker_configured(self) -> bool:
        return bool(self.rh_token or (self.rh_refresh_token and self.rh_device_token))


# ---------------------------------------------------------------------- normalized


@dataclass(frozen=True)
class Leg:
    key: str  # "XSP 766P 2026-10-19" or "MU" for shares
    symbol: str
    side: str  # buy | sell
    effect: str  # open | close | unknown
    ratio: int
    price: float | None  # per-unit fill price for this leg, None when not yet reported
    strike: float | None = None
    kind: str | None = None  # call | put | None for shares
    expiry: str | None = None

    @property
    def multiplier(self) -> int:
        return 100 if self.kind else 1

    def label(self) -> str:
        if self.kind:
            exp = self.expiry or "?"
            return f"{self.symbol} {self.strike:g}{self.kind[0].upper()} {exp}"
        return self.symbol


@dataclass(frozen=True)
class Order:
    id: str
    ts: str  # ISO, broker time
    state: str  # filled | partially_filled | cancelled | rejected | working
    legs: tuple[Leg, ...]
    qty: float  # filled quantity (contracts or shares), 0 when nothing filled yet
    net: float  # signed cash: negative = debit paid, positive = credit received
    fees: float
    direction: str  # debit | credit | flat

    @property
    def side(self) -> str:
        if len(self.legs) == 1:
            return self.legs[0].side.upper()
        effects = {leg.effect for leg in self.legs}
        if effects == {"open"}:
            return "OPEN"
        if effects == {"close"}:
            return "CLOSE"
        return "ROLL"

    @property
    def unit_price(self) -> float | None:
        if self.qty <= 0:
            return None
        return abs(self.net) / (self.qty * self.legs[0].multiplier)

    def legs_text(self) -> str:
        return " / ".join(f"{leg.side} {leg.ratio}x {leg.label()}" for leg in self.legs)


# ------------------------------------------------------------------- state machine


@dataclass
class Structure:
    order_id: str
    leg_keys: list[str]
    qty: float
    remaining: float
    max_loss: float  # total dollars at entry for the full qty


@dataclass
class Position:
    qty: float = 0.0  # signed, + long
    cost: float = 0.0  # signed cash paid for the open qty (negative when long)


@dataclass
class DayState:
    day: str
    realized: float = 0.0
    fees: float = 0.0
    trades: int = 0
    positions: dict[str, Position] = field(default_factory=dict)
    structures: dict[str, Structure] = field(default_factory=dict)
    seen_qty: dict[str, float] = field(default_factory=dict)  # order id -> booked qty
    seen_state: dict[str, str] = field(default_factory=dict)
    pushed: list[str] = field(default_factory=list)  # one-shot markers for the day

    @property
    def open_max_loss(self) -> float:
        return sum(
            s.max_loss * (s.remaining / s.qty) for s in self.structures.values() if s.qty > 0
        )

    @property
    def kernel(self) -> float:
        return max(0.0, -self.realized) + self.open_max_loss

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> DayState:
        state = cls(day=str(raw.get("day") or ""))
        state.realized = float(raw.get("realized") or 0.0)
        state.fees = float(raw.get("fees") or 0.0)
        state.trades = int(raw.get("trades") or 0)
        state.positions = {k: Position(**v) for k, v in (raw.get("positions") or {}).items()}
        state.structures = {k: Structure(**v) for k, v in (raw.get("structures") or {}).items()}
        state.seen_qty = {k: float(v) for k, v in (raw.get("seen_qty") or {}).items()}
        state.seen_state = dict(raw.get("seen_state") or {})
        state.pushed = list(raw.get("pushed") or [])
        return state


def spread_width(legs: tuple[Leg, ...]) -> float:
    """Widest same-type strike gap among option legs; 0 for a single option or shares."""
    width = 0.0
    for kind in ("call", "put"):
        strikes = [leg.strike for leg in legs if leg.kind == kind and leg.strike is not None]
        if len(strikes) >= 2:
            width = max(width, max(strikes) - min(strikes))
    return width


def max_loss_at_entry(order: Order) -> float | None:
    """Debit paid, or width minus credit for a spread. None for an undefined-risk credit."""
    if order.qty <= 0:
        return None
    multiplier = order.legs[0].multiplier
    if order.net <= 0:
        return -order.net
    width = spread_width(order.legs)
    if width <= 0:
        return None  # naked credit: no defined max loss (the kernel forbids it anyway)
    return max(0.0, width * multiplier * order.qty - order.net)


def book_fill(state: DayState, order: Order, new_qty: float) -> dict[str, Any]:
    """Apply ``new_qty`` units of ``order`` to positions, realized P&L and structures."""
    if new_qty <= 0:
        return {"realized_delta": 0.0}
    fraction = new_qty / order.qty if order.qty else 0.0
    realized_before = state.realized
    for leg in order.legs:
        pos = state.positions.setdefault(leg.key, Position())
        units = leg.ratio * new_qty
        signed_units = units if leg.side == "buy" else -units
        if leg.price is None:
            price = 0.0 if len(order.legs) > 1 else abs(order.net) / (order.qty * leg.multiplier)
        else:
            price = leg.price
        cash = -signed_units * price * leg.multiplier
        if pos.qty == 0 or (pos.qty > 0) == (signed_units > 0):
            pos.qty += signed_units
            pos.cost += cash
        else:
            closing = min(abs(signed_units), abs(pos.qty))
            avg_cost = pos.cost / pos.qty if pos.qty else 0.0  # cash per unit (signed)
            # closing `closing` units releases their cost and books the proceeds
            released = avg_cost * (closing if pos.qty > 0 else -closing)
            proceeds = cash * (closing / abs(signed_units))
            state.realized += proceeds + released
            pos.cost -= released
            pos.qty += closing if pos.qty < 0 else -closing
            leftover = abs(signed_units) - closing
            if leftover > 0:  # flipped through zero: the remainder opens a new position
                pos.qty += leftover if signed_units > 0 else -leftover
                pos.cost += cash * (leftover / abs(signed_units))
        if abs(pos.qty) < 1e-9:
            state.positions.pop(leg.key, None)
    fee_part = order.fees * fraction
    state.fees += fee_part
    state.realized -= fee_part
    state.trades += 1
    effects = {leg.effect for leg in order.legs}
    if effects == {"open"}:
        loss = max_loss_at_entry(order)
        if loss is not None:
            struct = state.structures.get(order.id)
            if struct is None:
                state.structures[order.id] = Structure(
                    order.id, [leg.key for leg in order.legs], order.qty, new_qty, loss
                )
            else:
                struct.remaining = min(struct.qty, struct.remaining + new_qty)
    else:
        closed_keys = {leg.key for leg in order.legs if leg.effect == "close"}
        for struct in list(state.structures.values()):
            if closed_keys & set(struct.leg_keys):
                # scale the structure's max loss by the share of its legs still open and
                # the share of its contracts still held on those legs
                open_units = [
                    min(abs(state.positions[key].qty), struct.qty)
                    for key in struct.leg_keys
                    if key in state.positions and abs(state.positions[key].qty) > 1e-9
                ]
                if not open_units:
                    struct.remaining = 0.0
                else:
                    share = sum(open_units) / (struct.qty * len(struct.leg_keys))
                    struct.remaining = min(struct.remaining, struct.qty * share)
            if struct.remaining <= 1e-9:
                state.structures.pop(struct.order_id, None)
    return {"realized_delta": state.realized - realized_before}


# ---------------------------------------------------------------------- rendering


def money(value: float | None) -> str:
    if value is None:
        return "n/a"
    sign = "-" if value < 0 else "+"
    return f"{sign}${abs(value):,.2f}"


def render_fill(order: Order, state: DayState) -> tuple[str, str]:
    price = order.unit_price
    price_text = "n/a" if price is None else f"{price:.2f}"
    net_word = "credit" if order.net > 0 else "debit"
    name = order.legs[0].label() if len(order.legs) == 1 else order.legs[0].symbol
    title = f"FILL {order.side} {order.qty:g} {name}"
    body = (
        f"FILL {order.side} {order.qty:g} {order.legs_text()} @ {price_text} "
        f"| fees {order.fees:.2f}\n"
        f"net {net_word} {money(order.net)} | realized today {money(state.realized)} | "
        f"kernel ${state.kernel:,.0f} / ${DAY_BUDGET:,.0f}"
    )
    return title, body


def render_cancel(order: Order) -> tuple[str, str]:
    return f"CANCELED {order.side} {order.legs[0].symbol}", (
        f"CANCELED {order.state} {order.legs_text()} (order {order.id[:8]})"
    )


def render_snapshot(state: DayState, marks: dict[str, float | None], when: str) -> tuple[str, str]:
    lines = []
    unreal = 0.0
    unreal_known = True
    for key, pos in sorted(state.positions.items()):
        mark = marks.get(key)
        mult = 100 if " " in key else 1
        if mark is None:
            unreal_known = False
            lines.append(f"{pos.qty:+g} {key} @ mark n/a")
        else:
            value = pos.qty * mark * mult
            unreal += value + pos.cost
            lines.append(f"{pos.qty:+g} {key} @ {mark:.2f}")
    body = "\n".join(lines) if lines else "flat"
    body += (
        f"\nrealized {money(state.realized)} | unrealized "
        f"{money(unreal) if unreal_known else 'n/a'}"
        f" | kernel ${state.kernel:,.0f} / ${DAY_BUDGET:,.0f}"
    )
    return f"{when} snapshot", body


def render_summary(
    state: DayState, marks: dict[str, float | None]
) -> tuple[str, str, dict[str, Any]]:
    unreal = 0.0
    known = True
    for key, pos in state.positions.items():
        mark = marks.get(key)
        if mark is None:
            known = False
            continue
        unreal += pos.qty * mark * (100 if " " in key else 1) + pos.cost
    summary = {
        "realized": round(state.realized, 2),
        "unrealized": round(unreal, 2) if known else None,
        "fees": round(state.fees, 2),
        "trades": state.trades,
        "kernel_used": round(state.kernel, 2),
        "open_positions": len(state.positions),
    }
    body = (
        f"realized {money(state.realized)} | unrealized {money(unreal) if known else 'n/a'} | "
        f"fees ${state.fees:.2f} | trades {state.trades} | kernel used ${state.kernel:,.0f} "
        f"of ${DAY_BUDGET:,.0f} | {len(state.positions)} open"
    )
    return "close summary", body, summary


# ------------------------------------------------------------------------- broker


class Broker(Protocol):
    def orders(self) -> list[Order]: ...

    def marks(self, keys: list[str]) -> dict[str, float | None]: ...


class ReadOnlyHTTP:
    """A session that can only GET. The trade watcher never holds anything else."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()

    def get(
        self, url: str, *, headers: dict[str, str], params: dict[str, Any] | None = None
    ) -> Any:
        response = self._session.get(url, headers=headers, params=params, timeout=20)
        response.raise_for_status()
        return response.json()


class RobinhoodClient:
    """Read-only Robinhood brokerage reads: option orders, stock orders, positions, marks.

    Authenticates with a bearer token. When a refresh token and device token are set it
    refreshes the access token on a 401 through Robinhood's OAuth endpoint (the one POST
    in this module, and it is not an order). There is no method here that writes.
    """

    def __init__(self, cfg: Config, http: ReadOnlyHTTP | None = None) -> None:
        self.cfg = cfg
        self.http = http or ReadOnlyHTTP()
        self.token = cfg.rh_token
        self._instruments: dict[str, dict[str, Any]] = {}
        self._session = requests.Session()

    # -- auth -------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def _refresh(self) -> None:
        if not (self.cfg.rh_refresh_token and self.cfg.rh_device_token):
            raise RuntimeError("robinhood token rejected and no refresh token is configured")
        response = self._session.post(
            f"{RH_BASE}/oauth2/token/",
            json={
                "grant_type": "refresh_token",
                "refresh_token": self.cfg.rh_refresh_token,
                "client_id": RH_CLIENT_ID,
                "device_token": self.cfg.rh_device_token,
                "scope": "internal",
            },
            timeout=20,
        )
        response.raise_for_status()
        self.token = str(response.json().get("access_token") or "")
        if not self.token:
            raise RuntimeError("robinhood token refresh returned no access token")

    def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return self.http.get(url, headers=self._headers(), params=params)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 401:
                self._refresh()
                return self.http.get(url, headers=self._headers(), params=params)
            raise

    def _paged(
        self, url: str, params: dict[str, Any] | None = None, max_pages: int = 5
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        next_url: str | None = url
        for _ in range(max_pages):
            if not next_url:
                break
            page = self._get(next_url, params if next_url == url else None)
            out.extend(page.get("results") or [])
            next_url = page.get("next")
        return out

    # -- instruments ----------------------------------------------------------
    def instrument(self, url: str) -> dict[str, Any]:
        if url not in self._instruments:
            self._instruments[url] = self._get(url)
        return self._instruments[url]

    # -- reads ----------------------------------------------------------------
    def orders(self) -> list[Order]:
        since = datetime.now(ET).date().isoformat()
        orders: list[Order] = []
        for raw in self._paged(f"{RH_BASE}/options/orders/", {"updated_at[gte]": since}):
            if (
                str(raw.get("updated_at") or "")[:10] < since
                and str(raw.get("created_at") or "")[:10] < since
            ):
                continue
            orders.append(normalize_option_order(raw, self.instrument, self._instruments))
        for raw in self._paged(f"{RH_BASE}/orders/", {"updated_at[gte]": since}):
            if str(raw.get("updated_at") or "")[:10] < since:
                continue
            orders.append(normalize_stock_order(raw, self.instrument))
        return orders

    def marks(self, keys: list[str]) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        option_urls = self._instrument_urls_for(keys)
        if option_urls:
            try:
                data = self._get(
                    f"{RH_BASE}/marketdata/options/", {"instruments": ",".join(option_urls)}
                )
                for item in data.get("results") or []:
                    if not item:
                        continue
                    key = option_urls.get(str(item.get("instrument")))
                    mark = item.get("adjusted_mark_price") or item.get("mark_price")
                    if key:
                        out[key] = float(mark) if mark not in (None, "") else None
            except Exception as exc:
                log.warning("option marks failed: %s", exc)
        for key in keys:
            out.setdefault(key, None)
        return out

    def _instrument_urls_for(self, keys: list[str]) -> dict[str, str]:
        wanted = set(keys)
        return {
            url: leg_key_from_instrument(inst)
            for url, inst in self._instruments.items()
            if leg_key_from_instrument(inst) in wanted
        }


def leg_key_from_instrument(inst: dict[str, Any]) -> str:
    symbol = str(inst.get("chain_symbol") or inst.get("symbol") or "?")
    if inst.get("strike_price") is None:
        return symbol
    strike = float(inst["strike_price"])
    kind = str(inst.get("type") or "?")[:1].upper()
    return f"{symbol} {strike:g}{kind} {inst.get('expiration_date')}"


def _state(raw_state: str, processed: float, total: float) -> str:
    s = (raw_state or "").lower()
    if s in ("cancelled", "canceled"):
        return "cancelled"
    if s in ("rejected", "failed"):
        return "rejected"
    if s == "filled":
        return "filled"
    if processed > 0 and processed < total:
        return "partially_filled"
    return "working"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_option_order(
    raw: dict[str, Any], lookup: Any, cache: dict[str, dict[str, Any]] | None = None
) -> Order:
    qty = _f(raw.get("processed_quantity"))
    total = _f(raw.get("quantity"))
    direction = str(raw.get("direction") or "debit")
    premium = _f(raw.get("processed_premium"))
    net = -premium if direction == "debit" else premium
    legs: list[Leg] = []
    for leg in raw.get("legs") or []:
        if leg.get("strike_price") is not None and leg.get("option_type"):
            inst = {
                "chain_symbol": raw.get("chain_symbol"),
                "strike_price": leg.get("strike_price"),
                "type": leg.get("option_type"),
                "expiration_date": leg.get("expiration_date"),
            }
            if leg.get("option_id") and cache is not None:
                cache[f"{RH_BASE}/options/instruments/{leg['option_id']}/"] = inst
        else:
            inst = lookup(str(leg.get("option")))
        execs = leg.get("executions") or []
        price: float | None = None
        if execs:
            filled = sum(_f(e.get("quantity")) for e in execs)
            if filled > 0:
                price = sum(_f(e.get("price")) * _f(e.get("quantity")) for e in execs) / filled
        legs.append(
            Leg(
                key=leg_key_from_instrument(inst),
                symbol=str(inst.get("chain_symbol") or raw.get("chain_symbol") or "?"),
                side=str(leg.get("side") or "buy"),
                effect=str(leg.get("position_effect") or "unknown"),
                ratio=int(_f(leg.get("ratio_quantity"), 1.0)),
                price=price,
                strike=_f(inst.get("strike_price")),
                kind=str(inst.get("type") or "call"),
                expiry=str(inst.get("expiration_date") or ""),
            )
        )
    fees = _f(raw.get("regulatory_fees")) + _f(raw.get("fees")) + _f(raw.get("contract_fees"))
    return Order(
        id=str(raw.get("id") or ""),
        ts=str(raw.get("updated_at") or raw.get("created_at") or ""),
        state=_state(str(raw.get("state") or ""), qty, total),
        legs=tuple(legs),
        qty=qty,
        net=net,
        fees=fees,
        direction=direction if qty else "flat",
    )


def normalize_stock_order(raw: dict[str, Any], lookup: Any) -> Order:
    inst = lookup(str(raw.get("instrument")))
    symbol = str(inst.get("symbol") or "?")
    qty = _f(raw.get("cumulative_quantity"))
    total = _f(raw.get("quantity"))
    price = _f(raw.get("average_price"), 0.0) or None
    side = str(raw.get("side") or "buy")
    net = 0.0 if price is None else (-qty * price if side == "buy" else qty * price)
    leg = Leg(key=symbol, symbol=symbol, side=side, effect="unknown", ratio=1, price=price)
    fees = _f(raw.get("fees"))
    return Order(
        id=str(raw.get("id") or ""),
        ts=str(raw.get("updated_at") or ""),
        state=_state(str(raw.get("state") or ""), qty, total),
        legs=(leg,),
        qty=qty,
        net=net,
        fees=fees,
        direction="debit" if net < 0 else "credit" if net > 0 else "flat",
    )


# ------------------------------------------------------------------------- ledger


class Ledger:
    """Appends rows to a CSV on disk and, when configured, to the Google Sheet."""

    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    def __init__(self, cfg: Config, session: requests.Session | None = None) -> None:
        self.cfg = cfg
        self.session = session or requests.Session()
        self.sheet_id = cfg.sheet_id
        self._creds: Any = None

    def append(self, row: dict[str, Any]) -> None:
        values = [row.get(col, "") for col in LEDGER_COLUMNS]
        self._append_csv(values)
        if self.cfg.service_account_json:
            try:
                self._append_sheet(values)
            except Exception as exc:
                log.warning("sheet append failed (row kept in csv): %s", exc)

    def _append_csv(self, values: list[Any]) -> None:
        path = self.cfg.ledger_csv
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with path.open("a", newline="") as handle:
            writer = csv.writer(handle)
            if new:
                writer.writerow(LEDGER_COLUMNS)
            writer.writerow(values)

    def _token(self) -> str:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        if self._creds is None:
            info = json.loads(self.cfg.service_account_json or "{}")
            self._creds = service_account.Credentials.from_service_account_info(
                info, scopes=self.SCOPES
            )
        if not self._creds.valid:
            self._creds.refresh(Request())
        return str(self._creds.token)

    def _ensure_sheet(self) -> str:
        if self.sheet_id:
            return self.sheet_id
        headers = {"Authorization": f"Bearer {self._token()}"}
        created = self.session.post(
            "https://sheets.googleapis.com/v4/spreadsheets",
            headers=headers,
            json={"properties": {"title": self.cfg.sheet_title}},
            timeout=20,
        )
        created.raise_for_status()
        self.sheet_id = str(created.json()["spreadsheetId"])
        self.session.post(
            f"https://sheets.googleapis.com/v4/spreadsheets/{self.sheet_id}/values/A1:append",
            headers=headers,
            params={"valueInputOption": "RAW"},
            json={"values": [LEDGER_COLUMNS]},
            timeout=20,
        ).raise_for_status()
        if self.cfg.sheet_share_email:
            self.session.post(
                f"https://www.googleapis.com/drive/v3/files/{self.sheet_id}/permissions",
                headers=headers,
                json={"type": "user", "role": "writer", "emailAddress": self.cfg.sheet_share_email},
                timeout=20,
            ).raise_for_status()
        log.info("created ledger sheet %s", self.sheet_id)
        return self.sheet_id

    def _append_sheet(self, values: list[Any]) -> None:
        sheet_id = self._ensure_sheet()
        self.session.post(
            f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/A1:append",
            headers={"Authorization": f"Bearer {self._token()}"},
            params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
            timeout=20,
            json={"values": [["" if v is None else v for v in values]]},
        ).raise_for_status()


# ------------------------------------------------------------------------ watcher


class Pusher(Protocol):
    def push(
        self,
        title: str,
        body: str,
        priority: str = "default",
        payload: dict[str, Any] | None = None,
    ) -> None: ...


class Watcher:
    def __init__(self, cfg: Config, broker: Broker, pusher: Pusher, ledger: Ledger) -> None:
        self.cfg = cfg
        self.broker = broker
        self.pusher = pusher
        self.ledger = ledger
        self.state = self._load()

    # -- persistence ----------------------------------------------------------
    def _load(self) -> DayState:
        try:
            raw = json.loads(self.cfg.state_path.read_text())
            return DayState.from_json(raw)
        except (OSError, ValueError, TypeError):
            return DayState(day="")

    def _save(self) -> None:
        path = self.cfg.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.state.to_json(), indent=1, sort_keys=True))
        tmp.replace(path)

    def _roll_day(self, today: date) -> None:
        if self.state.day != today.isoformat():
            carried = self.state.positions  # overnight positions keep their cost basis
            carried_structs = self.state.structures
            self.state = DayState(
                day=today.isoformat(), positions=carried, structures=carried_structs
            )

    # -- one poll -------------------------------------------------------------
    def poll(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now(UTC)
        local = now.astimezone(ET)
        self._roll_day(local.date())
        events: list[dict[str, Any]] = []
        orders = self.broker.orders()
        for order in sorted(orders, key=lambda o: o.ts):
            events.extend(self._apply(order, local))
        events.extend(self._scheduled(local))
        self._save()
        return events

    def _apply(self, order: Order, local: datetime) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        seen = self.state.seen_qty.get(order.id, 0.0)
        new_qty = order.qty - seen
        if new_qty > 1e-9 and order.unit_price is not None:
            result = book_fill(self.state, order, new_qty)
            self.state.seen_qty[order.id] = order.qty
            title, body = render_fill(order, self.state)
            self.pusher.push(title, body, "default", {"order": order.id})
            self.ledger.append(
                {
                    "ts_et": local.strftime("%Y-%m-%d %H:%M:%S"),
                    "order_id": order.id,
                    "legs": order.legs_text(),
                    "side": order.side,
                    "qty": new_qty,
                    "price": order.unit_price,
                    "net": round(order.net * (new_qty / order.qty), 2) if order.qty else order.net,
                    "fees": round(order.fees * (new_qty / order.qty), 2)
                    if order.qty
                    else order.fees,
                    "realized_day": round(self.state.realized, 2),
                    "kernel_used": round(self.state.kernel, 2),
                    "note": f"{order.direction} {order.state}",
                }
            )
            events.append({"kind": "fill", "order": order.id, **result})
            events.extend(self._kernel_checks())
        prev_state = self.state.seen_state.get(order.id)
        self.state.seen_state[order.id] = order.state
        if (
            order.state in ("cancelled", "rejected")
            and prev_state != order.state
            and (order.qty <= 0 or prev_state in ("working", "partially_filled"))
        ):
            title, body = render_cancel(order)
            self.pusher.push(title, body, "default", {"order": order.id})
            events.append({"kind": "cancel", "order": order.id})
        return events

    def _kernel_checks(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        kernel = self.state.kernel
        for step in KERNEL_STEPS:
            marker = f"kernel_{int(step * 100)}"
            if kernel >= self.cfg.day_budget * step and marker not in self.state.pushed:
                self.state.pushed.append(marker)
                pct = int(step * 100)
                title = f"KERNEL {pct}%: ${kernel:,.0f} of ${self.cfg.day_budget:,.0f}"
                body = (
                    f"realized {money(self.state.realized)} + open max loss "
                    f"${self.state.open_max_loss:,.0f}. "
                    + (
                        "day's $500 is spent: done until next week."
                        if step >= 1.0
                        else "size the next one down."
                    )
                )
                self.pusher.push(title, body, "high" if step >= 0.8 else "default")
                events.append({"kind": marker, "kernel": kernel})
        if self.state.realized >= self.cfg.day_budget and "doubled" not in self.state.pushed:
            self.state.pushed.append("doubled")
            self.pusher.push(
                "DAY DOUBLED, done for the day", f"realized {money(self.state.realized)}", "high"
            )
            events.append({"kind": "doubled", "realized": self.state.realized})
        return events

    def _scheduled(self, local: datetime) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        t = local.time()
        if t >= SNAPSHOT_AT and "snapshot" not in self.state.pushed and t < SUMMARY_AT:
            self.state.pushed.append("snapshot")
            marks = self.broker.marks(list(self.state.positions))
            title, body = render_snapshot(self.state, marks, "09:31")
            self.pusher.push(title, body)
            events.append({"kind": "snapshot"})
        if t >= SUMMARY_AT and "summary" not in self.state.pushed:
            self.state.pushed.append("summary")
            marks = self.broker.marks(list(self.state.positions))
            title, body, summary = render_summary(self.state, marks)
            self.pusher.push(title, body)
            self.ledger.append(
                {
                    "ts_et": local.strftime("%Y-%m-%d %H:%M:%S"),
                    "order_id": "",
                    "legs": "",
                    "side": "SUMMARY",
                    "qty": summary["trades"],
                    "price": "",
                    "net": summary["realized"],
                    "fees": summary["fees"],
                    "realized_day": summary["realized"],
                    "kernel_used": summary["kernel_used"],
                    "note": (
                        f"unrealized {summary['unrealized']} | {summary['open_positions']} open"
                    ),
                }
            )
            events.append({"kind": "summary", **summary})
        return events


def in_window(now: datetime) -> bool:
    local = now.astimezone(ET)
    return local.weekday() < 5 and WINDOW_OPEN <= local.time() < WINDOW_CLOSE


def run_forever(cfg: Config, broker: Broker, pusher: Pusher, ledger: Ledger) -> None:
    watcher = Watcher(cfg, broker, pusher, ledger)
    auth_failed_at: float | None = None
    while True:
        now = datetime.now(UTC)
        if not in_window(now):
            time.sleep(60)
            continue
        try:
            watcher.poll(now)
            auth_failed_at = None
        except Exception as exc:
            log.exception("trade poll failed")
            if auth_failed_at is None or time.time() - auth_failed_at > 3600:
                auth_failed_at = time.time()
                pusher.push("trade watcher error", f"{exc}"[:300], "default")
        time.sleep(cfg.poll_seconds)


def start_thread(pusher: Pusher) -> threading.Thread | None:
    """Start the trade watcher next to the vol alerts; None when no broker is configured."""
    cfg = Config.from_env()
    if not cfg.broker_configured:
        log.info("trade watcher idle: no ROBINHOOD_TOKEN configured")
        return None
    broker = RobinhoodClient(cfg)
    ledger = Ledger(cfg)
    thread = threading.Thread(
        target=run_forever, args=(cfg, broker, pusher, ledger), name="trade-watch", daemon=True
    )
    thread.start()
    log.info("trade watcher started: poll %ss, sheet %s", cfg.poll_seconds, cfg.sheet_id or "auto")
    return thread


def main() -> int:
    from app.vol_watch import Config as VolConfig
    from app.vol_watch import Notifier

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
    )
    cfg = Config.from_env()
    if not cfg.broker_configured:
        print("ROBINHOOD_TOKEN (or ROBINHOOD_REFRESH_TOKEN + ROBINHOOD_DEVICE_TOKEN) is required")
        return 2
    pusher = Notifier(VolConfig.from_env())
    run_forever(cfg, RobinhoodClient(cfg), pusher, Ledger(cfg))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
