"""Price levels and option-implied cheap/rich zones (SPEC 5.8).

The auction value area (POC/VAH/VAL) and the moving averages are reused verbatim
from :mod:`app.prism.levels` — Situate does not re-derive chart math. On top of
those, this module adds the SPEC 5.8 *zones*: combining the levels with the
option-implied distribution (:mod:`app.situate.implied`), the price at the 25th
implied quantile at 3 and 6 months is the **cheap zone** and the price at the
75th implied quantile is the **rich zone**. Zones are stated as price ranges, not
targets, and are ``None`` when the implied distribution for those horizons is
unavailable.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pandas as pd

LEVELS_VERSION = "1.0.0"

#: Horizons whose implied quantiles define the cheap/rich zones (SPEC 5.8).
_ZONE_HORIZONS: tuple[int, ...] = (3, 6)


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _close_series(history: Any) -> pd.Series:
    """Extract a positive close series from a history object or a bare Series."""
    if isinstance(history, pd.Series):
        series = history
    else:
        frame = getattr(history, "data", history)
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return pd.Series(dtype="float64")
        column = "Adj Close" if "Adj Close" in frame.columns else "Close"
        if column not in frame.columns:
            return pd.Series(dtype="float64")
        series = frame[column]
    series = pd.to_numeric(pd.Series(series), errors="coerce").dropna()
    return series[series > 0]


def moving_averages(history: Any) -> dict[str, float | None]:
    """Simple 20/50/200-day moving averages at the last bar (SPEC 5.8)."""
    close = _close_series(history)
    result: dict[str, float | None] = {"ma20": None, "ma50": None, "ma200": None}
    for key, window in (("ma20", 20), ("ma50", 50), ("ma200", 200)):
        if close.shape[0] >= window:
            result[key] = float(close.iloc[-window:].mean())
    return result


def distance_to_ma(
    current_price: float | None, mas: Mapping[str, float | None]
) -> dict[str, float | None]:
    """Fractional distance of price from each moving average (``price/ma − 1``)."""
    price = _finite(current_price)
    dist: dict[str, float | None] = {}
    for key in ("ma20", "ma50", "ma200"):
        ma = _finite(mas.get(key))
        dist[key] = (price / ma - 1.0) if (price is not None and ma) else None
    return dist


def implied_zones(
    implied: Mapping[str, Any] | None,
    *,
    spot: float | None,
    horizons: tuple[int, ...] = _ZONE_HORIZONS,
) -> dict[str, Any]:
    """Cheap/rich price zones from the 25th/75th implied quantiles at 3 & 6m.

    The zone spans the two horizons' quantile prices, so ``cheap_zone`` runs from
    the lower to the higher of the 3m/6m 25th-quantile prices, and ``rich_zone``
    from the lower to the higher of the 75th-quantile prices. ``None`` when the
    implied distribution is unavailable for those horizons.
    """
    empty = {"cheap_zone": None, "rich_zone": None}
    price = _finite(spot)
    if implied is None or price is None or price <= 0.0:
        return empty
    by_horizon = implied.get("by_horizon") or {}

    cheap_prices: list[float] = []
    rich_prices: list[float] = []
    used_horizons: list[int] = []
    for h in horizons:
        block = by_horizon.get(str(h))
        if not isinstance(block, Mapping):
            continue
        quantiles = block.get("quantiles") or {}
        q25 = _finite(quantiles.get("q25"))
        q75 = _finite(quantiles.get("q75"))
        if q25 is None or q75 is None:
            continue
        cheap_prices.append(price * (1.0 + q25))
        rich_prices.append(price * (1.0 + q75))
        used_horizons.append(h)

    if not cheap_prices or not rich_prices:
        return empty
    label = "-".join(f"{h}m" for h in used_horizons)
    return {
        "cheap_zone": {
            "price_lo": round(min(cheap_prices), 4),
            "price_hi": round(max(cheap_prices), 4),
            "horizon": label,
            "basis": "option-implied 25th quantile",
        },
        "rich_zone": {
            "price_lo": round(min(rich_prices), 4),
            "price_hi": round(max(rich_prices), 4),
            "horizon": label,
            "basis": "option-implied 75th quantile",
        },
    }


def build_levels(
    history: Any,
    *,
    implied: Mapping[str, Any] | None = None,
    current_price: float | None = None,
    period: str = "1y",
    sec_trend: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build ``packet["levels"]`` (SPEC 5.8).

    Auction levels + moving averages come from :mod:`app.prism.levels` (reused,
    not rebuilt); the cheap/rich zones come from the implied distribution. Every
    piece degrades independently: a failure in the Prism auction math cannot cost
    the zones, and a missing chain cannot cost the value area.
    """
    close = _close_series(history)
    price = _finite(current_price)
    if price is None and not close.empty:
        price = float(close.iloc[-1])

    section: dict[str, Any] = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "period": period,
        "current_price": price,
        "poc": None,
        "vah": None,
        "val": None,
        "ma20": None,
        "ma50": None,
        "ma200": None,
        "dist_to_ma": None,
        "key_levels": [],
        "cheap_zone": None,
        "rich_zone": None,
        "errors": [],
    }

    # Auction value area + key levels reused from Prism (needs a full history).
    try:
        from app.prism import levels as prism_levels

        prism_section = prism_levels.build_levels(
            history,
            period=period,
            sec_trend=sec_trend,
            profile=profile,
            current_price=price,
        )
        auction = prism_section.get("auction") or {}
        section["poc"] = _finite(auction.get("poc"))
        section["vah"] = _finite(auction.get("vah"))
        section["val"] = _finite(auction.get("val"))
        section["key_levels"] = prism_section.get("key_levels") or []
        for err in prism_section.get("errors") or []:
            section["errors"].append(err)
    except Exception as exc:  # noqa: BLE001 - auction math must not sink the zones
        section["errors"].append({"source": "levels.auction", "error": str(exc)})

    # Moving averages computed directly so the field names match the contract.
    try:
        mas = moving_averages(history)
        section.update(mas)
        section["dist_to_ma"] = distance_to_ma(price, mas)
    except Exception as exc:  # noqa: BLE001
        section["errors"].append({"source": "levels.moving_averages", "error": str(exc)})

    # Cheap/rich zones from the implied distribution.
    try:
        zones = implied_zones(implied, spot=price)
        section["cheap_zone"] = zones["cheap_zone"]
        section["rich_zone"] = zones["rich_zone"]
    except Exception as exc:  # noqa: BLE001
        section["errors"].append({"source": "levels.zones", "error": str(exc)})

    return section
