"""Realized and implied volatility for one ticker.

Realized volatility is computed from the same adjusted-close series the rest of
the packet uses, so the number in ``volatility`` and the number behind the
scenario mixture cannot disagree. Implied volatility comes from Massive's option
chain snapshot (``/v3/snapshot/options/{T}``), which returns per-contract
``implied_volatility`` and greeks.

The chain wrapper returns the nine strikes nearest the money, which is enough for
an ATM reading and a usable smile but not always enough to reach a true 25-delta
wing. Rather than extrapolate, the skew is reported with the deltas actually used
and is refused when the nearest available contract is too far from 0.25.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

TRADING_DAYS = 252.0

#: Trailing windows reported in ``volatility["realized"]``.
REALIZED_WINDOWS: dict[str, int] = {"1m": 21, "3m": 63, "6m": 126, "1y": 252}

#: Rolling window used for the vol-of-vol series and the percentile reference.
ROLLING_WINDOW = 21

#: How close to 0.25 a contract's |delta| must be for the wing to count.
DELTA_TOLERANCE = 0.10


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def log_returns(close: pd.Series) -> pd.Series:
    """Daily log returns of a positive close series."""
    prices = pd.to_numeric(pd.Series(close), errors="coerce").dropna()
    prices = prices[prices > 0]
    if prices.shape[0] < 2:
        return pd.Series(dtype="float64")
    return np.log(prices).diff().dropna()


def annualized_volatility(returns: pd.Series) -> float | None:
    """Sample standard deviation of daily log returns, annualised."""
    clean = pd.to_numeric(pd.Series(returns), errors="coerce").dropna()
    if clean.shape[0] < 3:
        return None
    value = float(clean.std(ddof=1) * math.sqrt(TRADING_DAYS))
    return value if math.isfinite(value) else None


def rolling_annualized_volatility(
    close: pd.Series, *, window: int = ROLLING_WINDOW
) -> pd.Series:
    """Rolling annualised realized volatility, indexed like ``close``."""
    returns = log_returns(close)
    if returns.empty:
        return pd.Series(dtype="float64")
    rolling = returns.rolling(int(window)).std(ddof=1) * math.sqrt(TRADING_DAYS)
    return rolling.dropna()


def realized_volatility(
    close: pd.Series, *, windows: Mapping[str, int] | None = None
) -> dict[str, Any]:
    """Per-window annualised volatility, its rolling average and its percentile.

    ``annualized`` is the volatility of the window itself. ``avg`` is the mean of
    the 21-day rolling volatility inside the window (what it felt like day to day).
    ``percentile`` places the window's reading in the full available history, so
    "20% vol" reads as calm or wild for that specific name.
    """
    spec = dict(windows or REALIZED_WINDOWS)
    returns = log_returns(close)
    rolling = rolling_annualized_volatility(close)
    result: dict[str, Any] = {}
    reference = rolling.to_numpy(dtype=float) if not rolling.empty else np.asarray([])
    for label, days in spec.items():
        window_returns = returns.iloc[-int(days):] if not returns.empty else returns
        annualized = annualized_volatility(window_returns)
        window_rolling = rolling.iloc[-int(days):] if not rolling.empty else rolling
        average = float(window_rolling.mean()) if not window_rolling.empty else None
        percentile: float | None = None
        if annualized is not None and reference.size >= 30:
            percentile = float((reference <= annualized).mean())
        result[label] = {
            "annualized": annualized,
            "avg": average,
            "percentile": percentile,
            "n": int(window_returns.shape[0]),
        }
    return result


def vol_of_vol(close: pd.Series, *, window: int = 252) -> float | None:
    """Annualised volatility of the 21-day realized-vol series itself."""
    rolling = rolling_annualized_volatility(close)
    if rolling.shape[0] < 40:
        return None
    tail = rolling.iloc[-int(window):]
    changes = np.log(tail[tail > 0]).diff().dropna()
    if changes.shape[0] < 20:
        return None
    value = float(changes.std(ddof=1) * math.sqrt(TRADING_DAYS))
    return value if math.isfinite(value) else None


def volatility_by_regime(
    close: pd.Series, regime_labels: pd.Series | None
) -> dict[str, Any]:
    """Average annualised volatility while each regime was in force."""
    if regime_labels is None or len(regime_labels) == 0:
        return {}
    rolling = rolling_annualized_volatility(close)
    if rolling.empty:
        return {}
    labels = pd.Series(regime_labels)
    if not isinstance(labels.index, pd.DatetimeIndex):
        converted = pd.to_datetime(labels.index, errors="coerce")
        labels = labels[converted.notna()]
        labels.index = converted[converted.notna()]
    aligned = labels.reindex(rolling.index).ffill()
    frame = pd.DataFrame({"vol": rolling, "regime": aligned}).dropna()
    if frame.empty:
        return {}
    out: dict[str, Any] = {}
    for label, group in frame.groupby("regime"):
        out[str(label)] = {
            "avg_annualized": float(group["vol"].mean()),
            "median_annualized": float(group["vol"].median()),
            "n_days": int(group.shape[0]),
        }
    return out


def third_friday(year: int, month: int) -> date:
    """The standard monthly option expiry (third Friday) for a month."""
    first = date(year, month, 1)
    # weekday(): Monday is 0, Friday is 4.
    offset = (4 - first.weekday()) % 7
    return first + timedelta(days=offset + 14)


def nearest_monthly_expiry(
    expirations: Sequence[str], *, as_of: date | None = None, min_days: int = 5
) -> str | None:
    """Pick the nearest standard monthly expiry at least ``min_days`` away.

    Weeklies dominate the front of the chain and their implied volatility is
    dominated by the next few sessions, which is not the number a memo wants.
    """
    today = as_of or datetime.now(UTC).date()
    monthlies: list[date] = []
    for raw in expirations:
        try:
            parsed = date.fromisoformat(str(raw).strip())
        except ValueError:
            continue
        if parsed == third_friday(parsed.year, parsed.month) and (parsed - today).days >= min_days:
            monthlies.append(parsed)
    if monthlies:
        return min(monthlies).isoformat()
    # No standard monthly in range: fall back to the furthest-out listed expiry
    # that clears ``min_days``, and let the caller report which one was used.
    candidates: list[date] = []
    for raw in expirations:
        try:
            parsed = date.fromisoformat(str(raw).strip())
        except ValueError:
            continue
        if (parsed - today).days >= min_days:
            candidates.append(parsed)
    return min(candidates).isoformat() if candidates else None


def smile_points(rows: Sequence[Mapping[str, Any]], current_price: float) -> list[dict[str, Any]]:
    """Flatten chain rows into per-strike call/put implied-volatility points."""
    points: list[dict[str, Any]] = []
    for row in rows:
        strike = _finite(row.get("strike"))
        if strike is None or current_price <= 0:
            continue
        moneyness = strike / current_price
        for kind in ("call", "put"):
            iv = _finite(row.get(f"{kind}_implied_volatility"))
            if iv is None:
                continue
            points.append(
                {
                    "strike": strike,
                    "moneyness": moneyness,
                    "iv": iv,
                    "type": kind,
                    "delta": _finite(row.get(f"{kind}_delta")),
                    "open_interest": _finite(row.get(f"{kind}_open_interest")),
                    "volume": _finite(row.get(f"{kind}_volume")),
                }
            )
    points.sort(key=lambda item: (float(item["strike"]), str(item["type"])))
    return points


def atm_implied_volatility(
    points: Sequence[Mapping[str, Any]], current_price: float
) -> dict[str, Any]:
    """Average the call and put implied volatility at the nearest strike."""
    if not points or current_price <= 0:
        return {"atm_iv": None, "atm_strike": None, "reason": "no implied volatility in the chain"}
    strikes = sorted({float(point["strike"]) for point in points})
    nearest = min(strikes, key=lambda strike: abs(strike - current_price))
    ivs = [float(point["iv"]) for point in points if float(point["strike"]) == nearest]
    if not ivs:
        return {"atm_iv": None, "atm_strike": nearest, "reason": "nearest strike carried no IV"}
    return {
        "atm_iv": float(sum(ivs) / len(ivs)),
        "atm_strike": nearest,
        "atm_moneyness": nearest / current_price,
        "reason": None,
    }


def delta_skew(points: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """25-delta put minus 25-delta call implied volatility, when reachable."""
    put = _closest_delta(points, "put", 0.25)
    call = _closest_delta(points, "call", 0.25)
    if put is None or call is None:
        return {
            "skew_25d": None,
            "put_delta": None,
            "call_delta": None,
            "reason": "the chain snapshot did not carry both wings with a delta",
        }
    put_gap = abs(abs(float(put["delta"])) - 0.25)
    call_gap = abs(abs(float(call["delta"])) - 0.25)
    if put_gap > DELTA_TOLERANCE or call_gap > DELTA_TOLERANCE:
        return {
            "skew_25d": None,
            "put_delta": float(put["delta"]),
            "call_delta": float(call["delta"]),
            "reason": (
                "nearest available wings are "
                f"{abs(float(put['delta'])):.2f} / {abs(float(call['delta'])):.2f} delta, "
                f"further than {DELTA_TOLERANCE:.2f} from 0.25"
            ),
        }
    return {
        "skew_25d": float(put["iv"]) - float(call["iv"]),
        "put_delta": float(put["delta"]),
        "call_delta": float(call["delta"]),
        "put_iv": float(put["iv"]),
        "call_iv": float(call["iv"]),
        "reason": None,
    }


def _closest_delta(
    points: Sequence[Mapping[str, Any]], kind: str, target: float
) -> Mapping[str, Any] | None:
    candidates = [
        point
        for point in points
        if str(point.get("type")) == kind and _finite(point.get("delta")) is not None
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda point: abs(abs(float(point["delta"])) - target))


def implied_volatility(
    client: Any,
    ticker: str,
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    """ATM implied volatility, 25-delta skew and the smile at the nearest monthly."""
    result: dict[str, Any] = {
        "provider": "massive",
        "expiry": None,
        "expiry_kind": None,
        "atm_iv": None,
        "atm_strike": None,
        "skew_25d": None,
        "smile": [],
        "underlying_price": None,
        "error": None,
    }
    try:
        expirations = list(client.get_expirations(ticker))
    except Exception as exc:  # noqa: BLE001
        expirations = []
        result["error"] = f"expirations unavailable: {exc}"

    expiry = nearest_monthly_expiry(expirations, as_of=as_of) if expirations else None
    try:
        chain = (
            client.get_option_chain(ticker, expiry)
            if expiry
            else client.get_option_chain(ticker)
        )
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"option chain unavailable: {exc}"
        return result

    used = str(getattr(chain, "expiry", "") or expiry or "")
    price = _finite(getattr(chain, "current_price", None)) or 0.0
    rows = list(getattr(chain, "rows", []) or [])
    points = smile_points(rows, price)
    atm = atm_implied_volatility(points, price)
    skew = delta_skew(points)

    kind = "unknown"
    if used:
        try:
            parsed = date.fromisoformat(used)
            kind = "monthly" if parsed == third_friday(parsed.year, parsed.month) else "weekly"
        except ValueError:
            kind = "unknown"

    result.update(
        {
            "expiry": used or None,
            "expiry_kind": kind,
            "expirations": [str(item) for item in expirations[:24]],
            "underlying_price": price or None,
            "atm_iv": atm.get("atm_iv"),
            "atm_strike": atm.get("atm_strike"),
            "atm_moneyness": atm.get("atm_moneyness"),
            "atm_reason": atm.get("reason"),
            "skew_25d": skew.get("skew_25d"),
            "skew_detail": skew,
            "smile": points,
            "n_strikes": len({float(point["strike"]) for point in points}),
        }
    )
    return result


def build_volatility(
    close: pd.Series,
    *,
    client: Any | None = None,
    ticker: str | None = None,
    regime_labels: pd.Series | None = None,
    as_of: date | None = None,
    include_implied: bool = True,
) -> dict[str, Any]:
    """Build ``packet["volatility"]``.

    Never raises: a missing option entitlement leaves ``implied.error`` set and
    the realized block intact.
    """
    realized = realized_volatility(close)
    section: dict[str, Any] = {
        "provider": "massive",
        "fetched_at": datetime.now(UTC).isoformat(),
        "realized": realized,
        "realized_current_21d": None,
        "vol_of_vol": vol_of_vol(close),
        "regime_avg": volatility_by_regime(close, regime_labels),
        "implied": None,
        "implied_error": None,
        "variance_risk_premium": None,
        "errors": [],
    }
    rolling = rolling_annualized_volatility(close)
    if not rolling.empty:
        section["realized_current_21d"] = float(rolling.iloc[-1])

    if include_implied and client is not None and ticker:
        implied = implied_volatility(client, ticker, as_of=as_of)
        section["implied"] = implied
        section["implied_error"] = implied.get("error")
        atm = _finite(implied.get("atm_iv"))
        realized_1m = _finite((realized.get("1m") or {}).get("annualized"))
        if atm is not None and realized_1m is not None:
            # What options are charging over what the stock actually did.
            section["variance_risk_premium"] = atm - realized_1m
    elif include_implied:
        section["implied_error"] = "no market client or ticker supplied for the option chain"

    return section
