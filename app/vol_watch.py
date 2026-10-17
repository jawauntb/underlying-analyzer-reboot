"""Volatility watcher: IV rank, implied-minus-realized, and the fast-drop setup.

A standalone worker (``python -m app.vol_watch``) that runs during US market hours,
pulls option chains for a watchlist every ``VOL_WATCH_CHAIN_MINUTES`` (default 15),
checks the index fast-drop setup every ``VOL_WATCH_TICK_MINUTES`` (default 5), and
pushes to a phone only when a signal *crosses* a threshold. Nothing is pushed on a
tick where the state did not change, so the phone stays quiet until something moves.

Metrics per ticker (all vol numbers are annualized percentage points):

* ``iv30``      ATM implied vol interpolated to 30 calendar days from the two
                nearest listed expiries (call/put ATM IV averaged).
* ``rv20``      close-to-close realized vol over the last 20 trading days.
* ``spread``    ``iv30 - rv20``; positive means the market is paying for more
                movement than the tape is delivering (sell time), negative means
                options are cheap against realized (buy gamma).
* ``iv_rank``   where today's iv30 sits inside the trailing-year range of iv30
                samples this watcher has stored (0 = at the low, 100 = at the high).
                Until ``VOL_WATCH_MIN_RANK_SAMPLES`` daily samples exist the rank is
                a proxy: iv30's percentile among the trailing 252 daily rv20 values,
                reported with ``iv_rank_basis = "rv_proxy"`` so the reader knows.
* ``skew``      5%-OTM put IV minus 5%-OTM call IV on the ~30d expiry; a put-spike
                shows up here before it shows up in iv30.

Fast drop: an index proxy (default SPY for XSP, plus QQQ) down at least
``VOL_WATCH_DROP_PCT`` inside the trailing ``VOL_WATCH_DROP_WINDOW_MINUTES`` while
^VIX is up at least ``VOL_WATCH_VIX_JUMP_PCT`` over the same window. One push per
``VOL_WATCH_DROP_COOLDOWN_MINUTES``.

Delivery: ntfy (``NTFY_TOPIC``), Pushover (``PUSHOVER_USER_KEY`` + ``PUSHOVER_APP_TOKEN``)
or a plain JSON webhook (``VOL_WATCH_WEBHOOK_URL``). Any subset may be configured.

State (last signal states, daily iv30 samples, cooldowns) lives in one JSON file at
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

TRADING_DAYS = 252.0


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
    min_rank_samples: int = 60
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
            min_rank_samples=_env_int("VOL_WATCH_MIN_RANK_SAMPLES", 60),
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


def rolling_realized_vol(closes: pd.Series, window: int = 20) -> pd.Series:
    series = pd.to_numeric(closes, errors="coerce").dropna()
    rets = np.log(series / series.shift(1))
    return (rets.rolling(window).std(ddof=1) * math.sqrt(TRADING_DAYS) * 100.0).dropna()


def atm_iv(calls: pd.DataFrame, puts: pd.DataFrame, spot: float) -> float | None:
    """Average of the call and put IV at the strike nearest spot. Returns points."""
    values: list[float] = []
    for frame in (calls, puts):
        iv = _iv_at_strike(frame, spot)
        if iv is not None:
            values.append(iv)
    if not values:
        return None
    return float(np.mean(values)) * 100.0


def _usable(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty or "impliedVolatility" not in frame:
        return pd.DataFrame()
    out = frame.copy()
    out["impliedVolatility"] = pd.to_numeric(out["impliedVolatility"], errors="coerce")
    out = out[(out["impliedVolatility"] > 0.01) & (out["impliedVolatility"] < 5.0)]
    if "bid" in out and "ask" in out:
        bid = pd.to_numeric(out["bid"], errors="coerce").fillna(0)
        ask = pd.to_numeric(out["ask"], errors="coerce").fillna(0)
        quoted = out[(bid > 0) & (ask > 0)]
        if not quoted.empty:
            out = quoted
    if "openInterest" in out:
        # Thin ETF chains (CANE, SOYB, VIXM) carry stale quotes on strikes nobody holds;
        # prefer strikes with open interest when any exist so the ATM pick is a real market.
        held = out[pd.to_numeric(out["openInterest"], errors="coerce").fillna(0) > 0]
        if not held.empty:
            out = held
    return out


def _iv_at_strike(frame: pd.DataFrame, target: float) -> float | None:
    usable = _usable(frame)
    if usable.empty:
        return None
    idx = (pd.to_numeric(usable["strike"], errors="coerce") - target).abs().idxmin()
    return float(usable.loc[idx, "impliedVolatility"])


def skew_points(
    calls: pd.DataFrame, puts: pd.DataFrame, spot: float, otm: float = 0.05
) -> float | None:
    """5%-OTM put IV minus 5%-OTM call IV, in points. Positive = puts bid over calls."""
    put_iv = _iv_at_strike(puts, spot * (1 - otm))
    call_iv = _iv_at_strike(calls, spot * (1 + otm))
    if put_iv is None or call_iv is None:
        return None
    return (put_iv - call_iv) * 100.0


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
            # interpolate total variance, then back to vol
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


def percentile_of(value: float, population: list[float]) -> float | None:
    if not population:
        return None
    below = sum(1 for p in population if p < value)
    return float(below / len(population) * 100.0)


def classify(value: float | None, high: float, low: float) -> str:
    if value is None:
        return "unknown"
    if value >= high:
        return "high"
    if value <= low:
        return "low"
    return "mid"


def crossings(
    ticker: str, metrics: dict[str, Any], state: State, cfg: Config
) -> list[dict[str, Any]]:
    """Return alerts for signals that changed band since the last tick, and update state."""
    alerts: list[dict[str, Any]] = []
    checks = {
        "iv_rank": classify(metrics.get("iv_rank"), cfg.iv_rank_high, cfg.iv_rank_low),
        "spread": classify(metrics.get("spread"), cfg.spread_rich, cfg.spread_cheap),
        "skew": "high" if (metrics.get("skew") or 0) >= cfg.skew_spike else "mid",
    }
    for name, band in checks.items():
        key = f"{ticker}:{name}"
        prev = state.signals.get(key)
        state.signals[key] = band
        if band in ("unknown", "mid") or prev == band:
            continue
        # First observation only fires when it lands in a band, so a fresh deploy
        # still tells him what is already rich or cheap, once.
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


# ----------------------------------------------------------------------------- data


class YahooData:
    """Thin wrapper so tests can stub the network with the same surface."""

    def __init__(self) -> None:
        import yfinance as yf  # imported lazily so pure functions import without it

        self._yf = yf

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

    def chain_metrics(self, ticker: str, spot: float, today: date) -> dict[str, Any]:
        """ATM IV at the two expiries bracketing 30 days, plus skew on the nearer one."""
        tk = self._yf.Ticker(self.symbol(ticker))
        expiries = list(tk.options)
        if not expiries:
            raise RuntimeError(f"{ticker}: no listed expiries")
        dated = []
        for exp in expiries:
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
        for days, exp in picks:
            chain = tk.option_chain(exp)
            iv = atm_iv(chain.calls, chain.puts, spot)
            if iv is not None:
                samples.append((float(days), iv))
            if skew is None:
                skew = skew_points(chain.calls, chain.puts, spot)
        iv30 = interpolate_30d(samples)
        return {"iv30": iv30, "skew": skew, "expiries": [e for _, e in picks], "samples": samples}


# ------------------------------------------------------------------------- delivery


def fmt(value: Any, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def render_signal(alert: dict[str, Any]) -> tuple[str, str, str]:
    """Return (title, body, priority) for a crossing alert."""
    t, s, band, m = alert["ticker"], alert["signal"], alert["band"], alert["metrics"]
    basis = " (rv proxy)" if m.get("iv_rank_basis") == "rv_proxy" else ""
    line = (
        f"spot {fmt(m.get('spot'), 2)}  iv30 {fmt(m.get('iv30'))}  rv20 {fmt(m.get('rv20'))}  "
        f"spread {fmt(m.get('spread'))}  rank {fmt(m.get('iv_rank'), 0)}{basis}  "
        f"skew {fmt(m.get('skew'))}"
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
    return title, f"{line}\n{hint}", "default"


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


def compute_metrics(
    ticker: str, data: YahooData, state: State, cfg: Config, today: date
) -> dict[str, Any]:
    closes = data.daily_closes(ticker)
    if closes.empty:
        raise RuntimeError(f"{ticker}: no daily history")
    spot = float(closes.iloc[-1])
    rv20 = realized_vol(closes, 20)
    chain = data.chain_metrics(ticker, spot, today)
    iv30 = chain["iv30"]
    metrics: dict[str, Any] = {
        "spot": round(spot, 2),
        "iv30": None if iv30 is None else round(iv30, 2),
        "rv20": None if rv20 is None else round(rv20, 2),
        "spread": None if iv30 is None or rv20 is None else round(iv30 - rv20, 2),
        "skew": None if chain["skew"] is None else round(chain["skew"], 2),
        "expiries": chain["expiries"],
        "iv_rank": None,
        "iv_rank_basis": None,
        "as_of": datetime.now(UTC).isoformat(),
    }
    if iv30 is not None:
        state.record_iv_sample(ticker, today, iv30)
        own = [v for k, v in state.iv_samples.get(ticker, {}).items() if k != today.isoformat()]
        if len(own) >= cfg.min_rank_samples:
            rank = iv_rank(iv30, own)
            metrics["iv_rank"], metrics["iv_rank_basis"] = (
                (None if rank is None else round(rank, 1)),
                "own_samples",
            )
        else:
            rv_hist = rolling_realized_vol(closes, 20).tail(252).tolist()
            pct = percentile_of(iv30, rv_hist)
            metrics["iv_rank"], metrics["iv_rank_basis"] = (
                (None if pct is None else round(pct, 1)),
                "rv_proxy",
            )
    return metrics


def run_chain_pass(
    cfg: Config, state: State, data: YahooData, notifier: Notifier, today: date
) -> list[dict[str, Any]]:
    fired: list[dict[str, Any]] = []
    for ticker in cfg.watchlist:
        try:
            metrics = compute_metrics(ticker, data, state, cfg, today)
        except Exception as exc:  # one bad name must not stop the pass
            log.warning("%s: chain metrics failed: %s", ticker, exc)
            continue
        state.last_metrics[ticker] = metrics
        log.info(
            "%s spot %s iv30 %s rv20 %s spread %s rank %s(%s) skew %s",
            ticker,
            metrics["spot"],
            metrics["iv30"],
            metrics["rv20"],
            metrics["spread"],
            metrics["iv_rank"],
            metrics["iv_rank_basis"],
            metrics["skew"],
        )
        for alert in crossings(ticker, metrics, state, cfg):
            title, body, prio = render_signal(alert)
            notifier.push(title, body, prio, alert)
            fired.append(alert)
    state.last_chain_run = datetime.now(UTC).isoformat()
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
        if last:
            since = now - datetime.fromisoformat(last)
            if since < timedelta(minutes=cfg.drop_cooldown_minutes):
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
    signals = run_chain_pass(cfg, state, data, notifier, today) if due else []
    state.save(cfg.state_path)
    return {"drops": drops, "signals": signals, "chains_ran": due}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
    )
    args = argv if argv is not None else sys.argv[1:]
    cfg = Config.from_env()
    state = State.load(cfg.state_path)
    data = YahooData()
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
