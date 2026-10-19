"""Volatility watcher: IV rank, implied-minus-realized, and the fast-drop setup.

A standalone worker (``python -m app.vol_watch``) that runs during US market hours,
pulls option chains for a watchlist every ``VOL_WATCH_CHAIN_MINUTES`` (default 15),
checks the index fast-drop setup every ``VOL_WATCH_TICK_MINUTES`` (default 5), and
pushes to a phone only when a signal *crosses* a threshold. Nothing is pushed on a
tick where the state did not change, so the phone stays quiet until something moves.

Quote discipline (every IV number below is built only from quotes that pass it):
bid > 0, ask >= bid, and (ask - bid) / mid < ``VOL_WATCH_MAX_REL_SPREAD`` (0.25).
IV is solved from the quote mid with Black-Scholes, never read from the vendor's
column. A ticker with no passing ATM quote is skipped for that pass and sends nothing.

Metrics per ticker (all vol numbers are annualized percentage points):

* ``iv30``      IV of the nearest-50-delta call and put (averaged) on each of the two
                expiries bracketing 30 calendar days, interpolated in variance-time.
* ``rv20``      close-to-close realized vol over the last 20 trading days.
* ``spread``    ``iv30 - rv20``.
* ``iv_rank``   where today's iv30 sits inside the trailing 252 stored daily iv30
                samples. Until 252 samples exist the rank is "n/a" and the rank signal
                does not fire. No realized-vol proxy.
* ``skew``      25-delta put IV minus 25-delta call IV on the nearer expiry; "n/a" when
                either side has no passing quote.
* ``zero_dte``  XSP only: the same-day ATM straddle mid as a percent of spot (the
                session implied move) against the move realized so far today.

Sanity gates (skip the ticker for the pass, send nothing): iv30 < 5; and for SPX,
XSP and SPY, |iv30 - VIX| > ``VOL_WATCH_VIX_GATE_POINTS`` (2).

Fast drop: an index proxy (SPY for XSP, plus QQQ) down at least ``VOL_WATCH_DROP_PCT``
inside the trailing ``VOL_WATCH_DROP_WINDOW_MINUTES`` while ^VIX is up at least
``VOL_WATCH_VIX_JUMP_PCT`` over the same window. One push per
``VOL_WATCH_DROP_COOLDOWN_MINUTES``.

Dedupe: at most one push per ticker per signal per ``VOL_WATCH_ALERT_COOLDOWN_MINUTES``
(30), on top of the band-change rule.

Delivery: ntfy (``NTFY_TOPIC``), Pushover (``PUSHOVER_USER_KEY`` + ``PUSHOVER_APP_TOKEN``)
or a plain JSON webhook (``VOL_WATCH_WEBHOOK_URL``). Any subset may be configured.

State (signal bands, daily iv30 samples, cooldowns) lives in one JSON file at
``VOL_WATCH_STATE_PATH`` so a restart does not re-fire every alert.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

log = logging.getLogger("vol_watch")

ET = ZoneInfo("America/New_York")
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)

DEFAULT_WATCHLIST = [
    "XSP",
    "MU",
    "TSLA",
    "LLY",
    "AVGO",
    "QQQ",
    "VIX",
    "SVXY",
    "VIXM",
    "EWZ",
    "SOYB",
    "CANE",
]

# Yahoo symbols for the names that are indexes rather than listed shares.
YAHOO_SYMBOLS = {"XSP": "^XSP", "VIX": "^VIX", "SPX": "^SPX"}

# Fast-drop proxies: the index names he trades map onto liquid intraday tapes.
DROP_PROXIES = {"XSP": "SPY", "SPX": "SPY", "QQQ": "QQQ"}

# Names whose 30-day IV must sit near VIX, which is itself a 30-day SPX IV.
VIX_GATED = {"SPX", "XSP", "SPY"}

# Names that get the same-day straddle line.
ZERO_DTE = {"XSP"}

TRADING_DAYS = 252.0
RISK_FREE = 0.04


# --------------------------------------------------------------------------- config


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


@dataclass(frozen=True)
class Config:
    watchlist: list[str]
    tick_minutes: int = 5
    chain_minutes: int = 15
    iv_rank_high: float = 50.0
    iv_rank_low: float = 20.0
    spread_rich: float = 8.0
    spread_cheap: float = -3.0
    skew_spike: float = 12.0
    rank_samples: int = 252
    max_rel_spread: float = 0.25
    min_iv: float = 5.0
    vix_gate_points: float = 2.0
    alert_cooldown_minutes: int = 30
    drop_pct: float = 0.75
    vix_jump_pct: float = 8.0
    drop_window_minutes: int = 60
    drop_cooldown_minutes: int = 60
    state_path: Path = Path("vol_watch_state.json")
    ntfy_topic: str | None = None
    ntfy_server: str = "https://ntfy.sh"
    pushover_user: str | None = None
    pushover_token: str | None = None
    webhook_url: str | None = None

    @classmethod
    def from_env(cls) -> Config:
        raw_list = os.environ.get("VOL_WATCH_TICKERS", "")
        watchlist = [t.strip().upper() for t in raw_list.split(",") if t.strip()] or list(
            DEFAULT_WATCHLIST
        )
        default_state = Path("/data/vol_watch_state.json")
        state_path = Path(
            os.environ.get("VOL_WATCH_STATE_PATH")
            or (default_state if default_state.parent.is_dir() else "vol_watch_state.json")
        )
        return cls(
            watchlist=watchlist,
            tick_minutes=max(1, _env_int("VOL_WATCH_TICK_MINUTES", 5)),
            chain_minutes=max(1, _env_int("VOL_WATCH_CHAIN_MINUTES", 15)),
            iv_rank_high=_env_float("VOL_WATCH_IV_RANK_HIGH", 50.0),
            iv_rank_low=_env_float("VOL_WATCH_IV_RANK_LOW", 20.0),
            spread_rich=_env_float("VOL_WATCH_SPREAD_RICH", 8.0),
            spread_cheap=_env_float("VOL_WATCH_SPREAD_CHEAP", -3.0),
            skew_spike=_env_float("VOL_WATCH_SKEW_SPIKE", 12.0),
            rank_samples=_env_int("VOL_WATCH_RANK_SAMPLES", 252),
            max_rel_spread=_env_float("VOL_WATCH_MAX_REL_SPREAD", 0.25),
            min_iv=_env_float("VOL_WATCH_MIN_IV", 5.0),
            vix_gate_points=_env_float("VOL_WATCH_VIX_GATE_POINTS", 2.0),
            alert_cooldown_minutes=_env_int("VOL_WATCH_ALERT_COOLDOWN_MINUTES", 30),
            drop_pct=_env_float("VOL_WATCH_DROP_PCT", 0.75),
            vix_jump_pct=_env_float("VOL_WATCH_VIX_JUMP_PCT", 8.0),
            drop_window_minutes=_env_int("VOL_WATCH_DROP_WINDOW_MINUTES", 60),
            drop_cooldown_minutes=_env_int("VOL_WATCH_DROP_COOLDOWN_MINUTES", 60),
            state_path=state_path,
            ntfy_topic=os.environ.get("NTFY_TOPIC") or None,
            ntfy_server=(os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/"),
            pushover_user=os.environ.get("PUSHOVER_USER_KEY") or None,
            pushover_token=os.environ.get("PUSHOVER_APP_TOKEN") or None,
            webhook_url=os.environ.get("VOL_WATCH_WEBHOOK_URL") or None,
        )


# ---------------------------------------------------------------------------- state


@dataclass
class State:
    signals: dict[str, str] = field(default_factory=dict)  # "TSLA:iv_rank" -> "high"
    iv_samples: dict[str, dict[str, float]] = field(default_factory=dict)  # tkr -> {date: iv}
    last_alert: dict[str, str] = field(default_factory=dict)  # "TSLA:iv_rank" -> iso ts
    last_drop_alert: dict[str, str] = field(default_factory=dict)  # proxy -> iso ts
    last_chain_run: str | None = None
    last_metrics: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> State:
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            return cls()
        state = cls()
        state.signals = dict(raw.get("signals") or {})
        state.iv_samples = {k: dict(v) for k, v in (raw.get("iv_samples") or {}).items()}
        state.last_alert = dict(raw.get("last_alert") or {})
        state.last_drop_alert = dict(raw.get("last_drop_alert") or {})
        state.last_chain_run = raw.get("last_chain_run")
        state.last_metrics = dict(raw.get("last_metrics") or {})
        return state

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=1, sort_keys=True))
        tmp.replace(path)

    def record_iv_sample(self, ticker: str, day: date, iv30: float, keep_days: int = 400) -> None:
        samples = self.iv_samples.setdefault(ticker, {})
        samples[day.isoformat()] = round(iv30, 4)
        cutoff = (day - timedelta(days=keep_days)).isoformat()
        for key in [k for k in samples if k < cutoff]:
            del samples[key]


# ----------------------------------------------------------------- black-scholes


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(
    spot: float, strike: float, t: float, vol: float, is_call: bool, r: float = RISK_FREE
) -> float:
    if t <= 0 or vol <= 0:
        intrinsic = max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)
        return intrinsic
    d1 = (math.log(spot / strike) + (r + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    if is_call:
        return spot * norm_cdf(d1) - strike * math.exp(-r * t) * norm_cdf(d2)
    return strike * math.exp(-r * t) * norm_cdf(-d2) - spot * norm_cdf(-d1)


def bs_delta(
    spot: float, strike: float, t: float, vol: float, is_call: bool, r: float = RISK_FREE
) -> float:
    if t <= 0 or vol <= 0:
        if is_call:
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0
    d1 = (math.log(spot / strike) + (r + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    return norm_cdf(d1) if is_call else norm_cdf(d1) - 1.0


def implied_vol(price: float, spot: float, strike: float, t: float, is_call: bool) -> float | None:
    """Solve Black-Scholes for vol by bisection. Returns a decimal (0.20 = 20%) or None."""
    if price <= 0 or spot <= 0 or strike <= 0 or t <= 0:
        return None
    intrinsic = bs_price(spot, strike, t, 0.0, is_call)
    if price <= intrinsic + 1e-9:
        return None
    lo, hi = 0.005, 5.0
    if bs_price(spot, strike, t, hi, is_call) < price:
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bs_price(spot, strike, t, mid, is_call) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-6:
            break
    vol = 0.5 * (lo + hi)
    return vol if 0.01 < vol < 4.0 else None


# ---------------------------------------------------------------------------- math


def is_market_open(now: datetime) -> bool:
    local = now.astimezone(ET)
    if local.weekday() >= 5:
        return False
    return MARKET_OPEN <= local.time() < MARKET_CLOSE


def realized_vol(closes: pd.Series, window: int = 20) -> float | None:
    """Annualized close-to-close realized vol over the last ``window`` returns, in points."""
    series = pd.to_numeric(closes, errors="coerce").dropna()
    if len(series) < window + 1:
        return None
    rets = np.log(series / series.shift(1)).dropna().tail(window)
    if len(rets) < window:
        return None
    return float(rets.std(ddof=1) * math.sqrt(TRADING_DAYS) * 100.0)


@dataclass(frozen=True)
class Quote:
    strike: float
    mid: float
    iv: float  # decimal
    delta: float


def passing_quotes(
    frame: pd.DataFrame, spot: float, t: float, is_call: bool, max_rel_spread: float
) -> list[Quote]:
    """Rows whose quote passes the filter, each with IV solved from its mid and its delta."""
    if frame is None or frame.empty:
        return []
    out: list[Quote] = []
    strikes = pd.to_numeric(frame.get("strike"), errors="coerce")
    bids = pd.to_numeric(frame.get("bid"), errors="coerce")
    asks = pd.to_numeric(frame.get("ask"), errors="coerce")
    for strike, bid, ask in zip(strikes, bids, asks, strict=False):
        if not (np.isfinite(strike) and np.isfinite(bid) and np.isfinite(ask)):
            continue
        if bid <= 0 or ask < bid:
            continue
        mid = 0.5 * (bid + ask)
        if mid <= 0 or (ask - bid) / mid >= max_rel_spread:
            continue
        iv = implied_vol(float(mid), spot, float(strike), t, is_call)
        if iv is None:
            continue
        out.append(
            Quote(float(strike), float(mid), iv, bs_delta(spot, float(strike), t, iv, is_call))
        )
    return out


def parity_forward(
    calls: pd.DataFrame, puts: pd.DataFrame, t: float, max_rel_spread: float, r: float = RISK_FREE
) -> float | None:
    """Forward from put-call parity at the strike where the call and put mids are closest.

    VIX options price off the VIX future and single names carry dividends, so Black-Scholes
    on spot mis-states every delta. F = K + (C - P) * e^{rT} needs no spot at all.
    """
    best: tuple[float, float] | None = None
    put_mids = {float(k): m for k, m in _filtered_mids(puts, max_rel_spread)}
    for strike, call_mid in _filtered_mids(calls, max_rel_spread):
        put_mid = put_mids.get(float(strike))
        if put_mid is None:
            continue
        gap = abs(call_mid - put_mid)
        if best is None or gap < best[0]:
            best = (gap, float(strike) + (call_mid - put_mid) * math.exp(r * t))
    return None if best is None else best[1]


def _filtered_mids(frame: pd.DataFrame, max_rel_spread: float) -> list[tuple[float, float]]:
    if frame is None or frame.empty:
        return []
    out: list[tuple[float, float]] = []
    strikes = pd.to_numeric(frame.get("strike"), errors="coerce")
    bids = pd.to_numeric(frame.get("bid"), errors="coerce")
    asks = pd.to_numeric(frame.get("ask"), errors="coerce")
    for strike, bid, ask in zip(strikes, bids, asks, strict=False):
        if not (np.isfinite(strike) and np.isfinite(bid) and np.isfinite(ask)):
            continue
        if bid <= 0 or ask < bid:
            continue
        mid = 0.5 * (bid + ask)
        if mid > 0 and (ask - bid) / mid < max_rel_spread:
            out.append((float(strike), float(mid)))
    return out


def nearest_delta(quotes: list[Quote], target: float, tolerance: float = 0.15) -> Quote | None:
    if not quotes:
        return None
    best = min(quotes, key=lambda q: abs(q.delta - target))
    return best if abs(best.delta - target) <= tolerance else None


def interpolate_30d(samples: list[tuple[float, float]], target_days: float = 30.0) -> float | None:
    """Linear interpolation of IV in variance-time to ``target_days``.

    ``samples`` are (days_to_expiry, iv_points). With one sample it is returned as-is;
    with two or more the bracketing pair is interpolated, or the nearest is used when
    the target lies outside the listed range.
    """
    clean = sorted((d, v) for d, v in samples if d > 0 and v is not None and v > 0)
    if not clean:
        return None
    if len(clean) == 1 or target_days <= clean[0][0]:
        return clean[0][1]
    if target_days >= clean[-1][0]:
        return clean[-1][1]
    for (d0, v0), (d1, v1) in zip(clean, clean[1:], strict=False):
        if d0 <= target_days <= d1:
            var0, var1 = (v0 / 100) ** 2 * d0, (v1 / 100) ** 2 * d1
            w = (target_days - d0) / (d1 - d0)
            var_t = var0 + w * (var1 - var0)
            return math.sqrt(max(var_t, 0.0) / target_days) * 100.0
    return clean[-1][1]


def iv_rank(current: float, history: list[float]) -> float | None:
    if len(history) < 2:
        return None
    lo, hi = min(history), max(history)
    if hi - lo < 1e-9:
        return None
    return float(max(0.0, min(100.0, (current - lo) / (hi - lo) * 100.0)))


def classify(value: float | None, high: float, low: float) -> str:
    if value is None:
        return "unknown"
    if value >= high:
        return "high"
    if value <= low:
        return "low"
    return "mid"


def crossings(
    ticker: str, metrics: dict[str, Any], state: State, cfg: Config, now: datetime
) -> list[dict[str, Any]]:
    """Alerts for signals that changed band since the last tick, deduped per ticker/signal.

    Band state is always updated. A crossing inside the cooldown window is recorded as
    the new band but not pushed, so the same signal cannot fire twice in 30 minutes.
    """
    alerts: list[dict[str, Any]] = []
    skew = metrics.get("skew")
    checks = {
        "iv_rank": classify(metrics.get("iv_rank"), cfg.iv_rank_high, cfg.iv_rank_low),
        "spread": classify(metrics.get("spread"), cfg.spread_rich, cfg.spread_cheap),
        "skew": "unknown" if skew is None else ("high" if skew >= cfg.skew_spike else "mid"),
    }
    for name, band in checks.items():
        key = f"{ticker}:{name}"
        prev = state.signals.get(key)
        state.signals[key] = band
        if band in ("unknown", "mid") or prev == band:
            continue
        last = state.last_alert.get(key)
        if last and now - datetime.fromisoformat(last) < timedelta(
            minutes=cfg.alert_cooldown_minutes
        ):
            continue
        state.last_alert[key] = now.isoformat()
        alerts.append({"ticker": ticker, "signal": name, "band": band, "metrics": metrics})
    return alerts


def fast_drop(
    proxy_bars: pd.Series,
    vix_bars: pd.Series,
    window_minutes: int,
    drop_pct: float,
    vix_jump_pct: float,
) -> dict[str, Any] | None:
    """Detect index down ``drop_pct`` inside the window while VIX is up ``vix_jump_pct``.

    Both series are intraday closes indexed by tz-aware timestamps. The move is measured
    from the window's high (worst drawdown inside the hour), not from the first bar, so a
    drop that starts mid-window still counts. VIX is measured from its window low.
    """
    if proxy_bars.empty or vix_bars.empty:
        return None
    end = proxy_bars.index[-1]
    start = end - pd.Timedelta(minutes=window_minutes)
    px = proxy_bars[proxy_bars.index >= start]
    vx = vix_bars[vix_bars.index >= start]
    if len(px) < 2 or len(vx) < 2:
        return None
    high = float(px.max())
    last = float(px.iloc[-1])
    drop = (last / high - 1.0) * 100.0
    vix_low = float(vx.min())
    vix_last = float(vx.iloc[-1])
    jump = (vix_last / vix_low - 1.0) * 100.0
    if drop <= -drop_pct and jump >= vix_jump_pct:
        return {
            "drop_pct": round(drop, 2),
            "vix_jump_pct": round(jump, 2),
            "proxy_last": round(last, 2),
            "proxy_high": round(high, 2),
            "vix_last": round(vix_last, 2),
            "vix_low": round(vix_low, 2),
            "window_minutes": window_minutes,
            "as_of": end.isoformat(),
        }
    return None


def session_move(today_closes: pd.Series) -> dict[str, float] | None:
    """Realized move so far today from intraday closes: signed move and high-low range, %."""
    series = pd.to_numeric(today_closes, errors="coerce").dropna()
    if len(series) < 2:
        return None
    first, last = float(series.iloc[0]), float(series.iloc[-1])
    if first <= 0:
        return None
    return {
        "move_pct": round((last / first - 1.0) * 100.0, 2),
        "range_pct": round((float(series.max()) - float(series.min())) / first * 100.0, 2),
    }


# ----------------------------------------------------------------------------- data


class YahooData:
    """Thin wrapper so tests can stub the network with the same surface."""

    def __init__(self, max_rel_spread: float = 0.25) -> None:
        import yfinance as yf  # imported lazily so pure functions import without it

        self._yf = yf
        self.max_rel_spread = max_rel_spread

    def symbol(self, ticker: str) -> str:
        return YAHOO_SYMBOLS.get(ticker, ticker)

    def daily_closes(self, ticker: str, period: str = "2y") -> pd.Series:
        hist = self._yf.Ticker(self.symbol(ticker)).history(period=period, interval="1d")
        if hist.empty:
            return pd.Series(dtype=float)
        return hist["Close"].dropna()

    def intraday_closes(self, ticker: str) -> pd.Series:
        hist = self._yf.Ticker(self.symbol(ticker)).history(period="2d", interval="5m")
        if hist.empty:
            return pd.Series(dtype=float)
        return hist["Close"].dropna()

    def today_closes(self, ticker: str, today: date) -> pd.Series:
        bars = self.intraday_closes(ticker)
        if bars.empty:
            return bars
        local_days = bars.index.tz_convert(ET).date
        return bars[local_days == today]

    def vix_level(self) -> float | None:
        bars = self.intraday_closes("VIX")
        if not bars.empty:
            return float(bars.iloc[-1])
        closes = self.daily_closes("VIX", period="5d")
        return float(closes.iloc[-1]) if not closes.empty else None

    def _expiry_chain(self, ticker: str, expiry: str, spot: float, days: float) -> dict[str, Any]:
        tk = self._yf.Ticker(self.symbol(ticker))
        chain = tk.option_chain(expiry)
        t = max(days, 0.5) / 365.0
        forward = parity_forward(chain.calls, chain.puts, t, self.max_rel_spread)
        # Price off the parity forward (discounted, so the BS helpers see a dividend- and
        # futures-consistent "spot"); fall back to spot when no strike has both sides quoted.
        underlying = forward * math.exp(-RISK_FREE * t) if forward else spot
        calls = passing_quotes(chain.calls, underlying, t, True, self.max_rel_spread)
        puts = passing_quotes(chain.puts, underlying, t, False, self.max_rel_spread)
        return {
            "forward": forward,
            "atm_call": nearest_delta(calls, 0.5),
            "atm_put": nearest_delta(puts, -0.5),
            "call_25": nearest_delta(calls, 0.25, tolerance=0.08),
            "put_25": nearest_delta(puts, -0.25, tolerance=0.08),
        }

    def chain_metrics(self, ticker: str, spot: float, today: date) -> dict[str, Any]:
        """iv30 from the 50-delta call/put on the expiries bracketing 30 days, plus skew.

        Raises when no expiry yields a passing ATM quote, which the caller treats as
        "skip this ticker, send nothing".
        """
        tk = self._yf.Ticker(self.symbol(ticker))
        dated: list[tuple[int, str]] = []
        for exp in tk.options:
            try:
                d = (date.fromisoformat(exp) - today).days
            except ValueError:
                continue
            if d >= 1:
                dated.append((d, exp))
        dated.sort()
        if not dated:
            raise RuntimeError(f"{ticker}: no future expiries")
        below = [x for x in dated if x[0] <= 30]
        above = [x for x in dated if x[0] > 30]
        picks = ([below[-1]] if below else []) + ([above[0]] if above else [])
        if len(picks) == 1 and len(dated) > 1:
            picks = dated[:2]
        samples: list[tuple[float, float]] = []
        skew: float | None = None
        skew_done = False
        for days, exp in picks:
            legs = self._expiry_chain(ticker, exp, spot, float(days))
            atm = [q.iv for q in (legs["atm_call"], legs["atm_put"]) if q is not None]
            if atm:
                samples.append((float(days), float(np.mean(atm)) * 100.0))
            if not skew_done:
                skew_done = True
                if legs["put_25"] is not None and legs["call_25"] is not None:
                    skew = (legs["put_25"].iv - legs["call_25"].iv) * 100.0
        iv30 = interpolate_30d(samples)
        if iv30 is None:
            raise RuntimeError(f"{ticker}: no ATM quote passed the filter")
        return {"iv30": iv30, "skew": skew, "expiries": [e for _, e in picks], "samples": samples}

    def zero_dte(self, ticker: str, spot: float, today: date) -> dict[str, Any] | None:
        """Same-day ATM straddle mid vs the move realized so far today. None if no 0DTE."""
        tk = self._yf.Ticker(self.symbol(ticker))
        if today.isoformat() not in tk.options:
            return None
        local_now = datetime.now(ET)
        close_at = datetime.combine(today, MARKET_CLOSE, tzinfo=ET)
        hours_left = max((close_at - local_now).total_seconds() / 3600.0, 0.25)
        legs = self._expiry_chain(ticker, today.isoformat(), spot, hours_left / 24.0)
        call, put = legs["atm_call"], legs["atm_put"]
        if call is None or put is None:
            return None
        straddle = call.mid + put.mid
        realized = session_move(self.today_closes(ticker, today))
        return {
            "straddle_mid": round(straddle, 2),
            "implied_move_pct": round(straddle / spot * 100.0, 2),
            "realized_move_pct": None if realized is None else realized["move_pct"],
            "realized_range_pct": None if realized is None else realized["range_pct"],
        }


# ------------------------------------------------------------------------- delivery


def fmt(value: Any, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def zero_dte_line(z: dict[str, Any] | None) -> str:
    if not z:
        return "0dte n/a"
    return (
        f"0dte straddle {fmt(z.get('straddle_mid'), 2)} = "
        f"{fmt(z.get('implied_move_pct'), 2)}% implied, "
        f"realized {fmt(z.get('realized_move_pct'), 2)}% "
        f"(range {fmt(z.get('realized_range_pct'), 2)}%)"
    )


def render_signal(alert: dict[str, Any]) -> tuple[str, str, str]:
    """Return (title, body, priority) for a crossing alert."""
    t, s, band, m = alert["ticker"], alert["signal"], alert["band"], alert["metrics"]
    rank = m.get("iv_rank")
    rank_text = "rank n/a" if rank is None else f"rank {rank:.0f}"
    line = (
        f"spot {fmt(m.get('spot'), 2)}  iv30 {fmt(m.get('iv30'))}  rv20 {fmt(m.get('rv20'))}  "
        f"spread {fmt(m.get('spread'))}  {rank_text}  skew25 {fmt(m.get('skew'))}"
    )
    if s == "iv_rank":
        title = f"{t} iv rank {'rich' if band == 'high' else 'cheap'}"
        hint = (
            "sell time: credit spread / iron fly"
            if band == "high"
            else "buy gamma: long option first"
        )
    elif s == "spread":
        title = f"{t} implied {'>>' if band == 'high' else '<'} realized"
        hint = (
            "market paying for more movement than the tape delivers: sell premium"
            if band == "high"
            else "options cheap vs realized: long gamma is cheap here"
        )
    else:
        title = f"{t} put skew spike"
        hint = "puts bid over calls: sell put spread into the spike or convert longs to zero-cost"
    body = f"{line}\n{hint}"
    if t in ZERO_DTE:
        body += "\n" + zero_dte_line(m.get("zero_dte"))
    return title, body, "default"


def render_drop(name: str, hit: dict[str, Any]) -> tuple[str, str, str]:
    title = f"FAST DROP {name}: {hit['drop_pct']}% / vix +{hit['vix_jump_pct']}%"
    body = (
        f"{name} {hit['proxy_high']} -> {hit['proxy_last']} inside {hit['window_minutes']}m, "
        f"vix {hit['vix_low']} -> {hit['vix_last']}. put-spike setup: if you are long puts, "
        f"sell the further otm put now (zero-cost spread). if flat, gamma is already priced; "
        f"sell the put spread, don't chase."
    )
    return title, body, "high"


class Notifier:
    def __init__(self, cfg: Config, session: requests.Session | None = None) -> None:
        self.cfg = cfg
        self.session = session or requests.Session()
        self.sent: list[dict[str, Any]] = []

    @property
    def configured(self) -> bool:
        c = self.cfg
        return bool(c.ntfy_topic or (c.pushover_user and c.pushover_token) or c.webhook_url)

    def push(
        self,
        title: str,
        body: str,
        priority: str = "default",
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.sent.append({"title": title, "body": body, "priority": priority})
        c = self.cfg
        if c.ntfy_topic:
            try:
                self.session.post(
                    f"{c.ntfy_server}/{c.ntfy_topic}",
                    data=body.encode("utf-8"),
                    headers={
                        "Title": title.encode("ascii", "ignore").decode(),
                        "Priority": "high" if priority == "high" else "default",
                        "Tags": "rotating_light"
                        if priority == "high"
                        else "chart_with_upwards_trend",
                    },
                    timeout=15,
                ).raise_for_status()
            except requests.RequestException as exc:
                log.warning("ntfy push failed: %s", exc)
        if c.pushover_user and c.pushover_token:
            try:
                self.session.post(
                    "https://api.pushover.net/1/messages.json",
                    data={
                        "token": c.pushover_token,
                        "user": c.pushover_user,
                        "title": title,
                        "message": body,
                        "priority": 1 if priority == "high" else 0,
                    },
                    timeout=15,
                ).raise_for_status()
            except requests.RequestException as exc:
                log.warning("pushover push failed: %s", exc)
        if c.webhook_url:
            try:
                self.session.post(
                    c.webhook_url,
                    json={
                        "title": title,
                        "body": body,
                        "priority": priority,
                        "payload": payload or {},
                    },
                    timeout=15,
                ).raise_for_status()
            except requests.RequestException as exc:
                log.warning("webhook push failed: %s", exc)


# ------------------------------------------------------------------------------ run


class SkipTicker(RuntimeError):
    """The pass produced no trustworthy number for this ticker; send nothing."""


def compute_metrics(
    ticker: str,
    data: YahooData,
    state: State,
    cfg: Config,
    today: date,
    vix_level: float | None,
) -> dict[str, Any]:
    closes = data.daily_closes(ticker)
    if closes.empty:
        raise SkipTicker(f"{ticker}: no daily history")
    spot = float(closes.iloc[-1])
    rv20 = realized_vol(closes, 20)
    chain = data.chain_metrics(ticker, spot, today)
    iv30 = float(chain["iv30"])
    if iv30 < cfg.min_iv:
        raise SkipTicker(f"{ticker}: iv30 {iv30:.1f} below {cfg.min_iv} floor")
    if ticker in VIX_GATED:
        if vix_level is None:
            raise SkipTicker(f"{ticker}: VIX level unavailable for the sanity gate")
        if abs(iv30 - vix_level) > cfg.vix_gate_points:
            raise SkipTicker(
                f"{ticker}: iv30 {iv30:.1f} vs VIX {vix_level:.1f} "
                f"outside {cfg.vix_gate_points} pts"
            )
    state.record_iv_sample(ticker, today, iv30)
    own = [v for k, v in state.iv_samples.get(ticker, {}).items() if k != today.isoformat()]
    rank = iv_rank(iv30, own[-cfg.rank_samples :]) if len(own) >= cfg.rank_samples else None
    zero = data.zero_dte(ticker, spot, today) if ticker in ZERO_DTE else None
    return {
        "spot": round(spot, 2),
        "iv30": round(iv30, 2),
        "rv20": None if rv20 is None else round(rv20, 2),
        "spread": None if rv20 is None else round(iv30 - rv20, 2),
        "skew": None if chain["skew"] is None else round(float(chain["skew"]), 2),
        "iv_rank": None if rank is None else round(rank, 1),
        "iv_rank_samples": len(own),
        "expiries": chain["expiries"],
        "zero_dte": zero,
        "vix": vix_level,
        "as_of": datetime.now(UTC).isoformat(),
    }


def run_chain_pass(
    cfg: Config, state: State, data: YahooData, notifier: Notifier, today: date, now: datetime
) -> list[dict[str, Any]]:
    fired: list[dict[str, Any]] = []
    try:
        vix_level = data.vix_level()
    except Exception as exc:
        log.warning("VIX level failed: %s", exc)
        vix_level = None
    for ticker in cfg.watchlist:
        try:
            metrics = compute_metrics(ticker, data, state, cfg, today, vix_level)
        except SkipTicker as exc:
            log.info("skip %s", exc)
            continue
        except Exception as exc:  # one bad name must not stop the pass
            log.warning("%s: chain metrics failed, skipping: %s", ticker, exc)
            continue
        state.last_metrics[ticker] = metrics
        log.info(
            "%s spot %s iv30 %s rv20 %s spread %s rank %s (%s/%s samples) skew25 %s%s",
            ticker,
            metrics["spot"],
            metrics["iv30"],
            metrics["rv20"],
            metrics["spread"],
            "n/a" if metrics["iv_rank"] is None else metrics["iv_rank"],
            metrics["iv_rank_samples"],
            cfg.rank_samples,
            "n/a" if metrics["skew"] is None else metrics["skew"],
            f" | {zero_dte_line(metrics['zero_dte'])}" if ticker in ZERO_DTE else "",
        )
        for alert in crossings(ticker, metrics, state, cfg, now):
            title, body, prio = render_signal(alert)
            notifier.push(title, body, prio, alert)
            fired.append(alert)
    state.last_chain_run = now.isoformat()
    return fired


def run_drop_pass(
    cfg: Config, state: State, data: YahooData, notifier: Notifier, now: datetime
) -> list[dict[str, Any]]:
    proxies = sorted({DROP_PROXIES[t] for t in cfg.watchlist if t in DROP_PROXIES})
    if not proxies:
        return []
    fired: list[dict[str, Any]] = []
    try:
        vix = data.intraday_closes("VIX")
    except Exception as exc:
        log.warning("VIX intraday failed: %s", exc)
        return []
    for proxy in proxies:
        try:
            bars = data.intraday_closes(proxy)
        except Exception as exc:
            log.warning("%s intraday failed: %s", proxy, exc)
            continue
        hit = fast_drop(bars, vix, cfg.drop_window_minutes, cfg.drop_pct, cfg.vix_jump_pct)
        if not hit:
            continue
        last = state.last_drop_alert.get(proxy)
        if last and now - datetime.fromisoformat(last) < timedelta(
            minutes=cfg.drop_cooldown_minutes
        ):
            continue
        state.last_drop_alert[proxy] = now.isoformat()
        title, body, prio = render_drop(proxy, hit)
        notifier.push(title, body, prio, hit)
        fired.append({"proxy": proxy, **hit})
    return fired


def tick(
    cfg: Config,
    state: State,
    data: YahooData,
    notifier: Notifier,
    now: datetime | None = None,
    force_chains: bool = False,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    today = now.astimezone(ET).date()
    drops = run_drop_pass(cfg, state, data, notifier, now)
    due = (
        force_chains
        or state.last_chain_run is None
        or (
            now - datetime.fromisoformat(state.last_chain_run)
            >= timedelta(minutes=cfg.chain_minutes)
        )
    )
    signals = run_chain_pass(cfg, state, data, notifier, today, now) if due else []
    state.save(cfg.state_path)
    return {"drops": drops, "signals": signals, "chains_ran": due}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
    )
    args = argv if argv is not None else sys.argv[1:]
    cfg = Config.from_env()
    state = State.load(cfg.state_path)
    data = YahooData(max_rel_spread=cfg.max_rel_spread)
    notifier = Notifier(cfg)
    log.info(
        "vol_watch watchlist=%s state=%s push=%s",
        cfg.watchlist,
        cfg.state_path,
        notifier.configured,
    )
    if "--once" in args:
        result = tick(cfg, state, data, notifier, force_chains=True)
        print(json.dumps({"fired": result, "metrics": state.last_metrics}, indent=1, default=str))
        return 0
    if "--test-push" in args:
        notifier.push("vol_watch test", "if you see this, pushes reach your phone", "default")
        return 0
    while True:
        now = datetime.now(UTC)
        if is_market_open(now):
            try:
                tick(cfg, state, data, notifier, now)
            except Exception:
                log.exception("tick failed")
            time.sleep(cfg.tick_minutes * 60)
        else:
            time.sleep(60)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
