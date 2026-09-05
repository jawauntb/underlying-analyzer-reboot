"""Merge the forward-return distribution the memo reads (``odds``) and the
bull/neutral/bear ``scenarios`` block.

``odds`` is the single distribution every downstream reader (memo, zones,
scenarios, chat) quotes, per horizon. It is assembled from three sources, in
order of preference:

* the ``stack`` (SPEC 5.7) when its gates passed and it published usable
  quantiles — its target is the forward *excess* return over the industry ETF,
  so those quantiles are lifted to a total-return basis by adding the industry
  ETF's own shrunk base-rate median (a documented proxy, never a fabricated
  number);
* otherwise the equal-weight blend of the shrunk conditional base rate
  (SPEC 5.3) and the option-implied real-world quantiles (SPEC 5.4), which is the
  guaranteed ship state (the stack falling back is acceptable);
* otherwise whichever of the two is present on its own.

Every quantity is a decimal fraction (``0.034`` is ``3.4%``). Nothing is
invented: a horizon with no usable source is ``None`` with a stated reason.

``scenarios`` (SPEC §6.6) frames the same distribution as bull / neutral / bear
at 3, 6 and 12 months, each defined by a *state*, the corresponding quantile of
the odds distribution, and the top-two exposure drivers (the largest-magnitude
basket betas). It never emits a point price target.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.situate.contract import HORIZONS, QUANTILE_KEYS

ODDS_VERSION = "1.0.0"

#: Horizons framed as bull/neutral/bear scenarios (SPEC §6.6).
SCENARIO_HORIZONS: tuple[int, ...] = (3, 6, 12)

#: Which odds quantile each scenario's central path reads.
_SCENARIO_QUANTILE: dict[str, str] = {"bull": "q75", "neutral": "q50", "bear": "q25"}

#: A short, honest description of the state each scenario conditions on.
_SCENARIO_STATE: dict[str, str] = {
    "bull": "risk-on: volatility low, trend up",
    "neutral": "the current state",
    "bear": "risk-off: volatility high, trend down",
}


def _finite(value: Any) -> float | None:
    """Coerce to a finite float, or ``None`` (bools are never numbers here)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    import math

    return number if math.isfinite(number) else None


def _quantile_map(block: Mapping[str, Any] | None) -> dict[str, float] | None:
    """A ``{q05..q95}`` map from a quantile block, or ``None`` if incomplete."""
    if not isinstance(block, Mapping):
        return None
    out: dict[str, float] = {}
    for key in QUANTILE_KEYS:
        value = _finite(block.get(key))
        if value is None:
            return None
        out[key] = value
    return out


def _blend(a: Mapping[str, float], b: Mapping[str, float]) -> dict[str, float]:
    """Equal-weight blend of two complete quantile maps."""
    return {key: round((a[key] + b[key]) / 2.0, 6) for key in QUANTILE_KEYS}


def _p_up_from_quantiles(quantiles: Mapping[str, float]) -> float | None:
    """P(return > 0) by linear interpolation across the 5 quantile knots.

    The 5/25/50/75/95 quantiles define a piecewise-linear inverse-CDF; inverting
    it at ``return == 0`` gives the cumulative probability of a loss, so ``p_up``
    is one minus that. Returns ``None`` when zero lies outside the knots (the
    whole distribution is one-signed), reported as ``1.0``/``0.0`` accordingly.
    """
    levels = [0.05, 0.25, 0.50, 0.75, 0.95]
    values = [quantiles[key] for key in QUANTILE_KEYS]
    if values[0] > 0.0:
        return 1.0
    if values[-1] < 0.0:
        return 0.0
    # Find the segment bracketing return == 0 and interpolate the CDF level.
    for i in range(len(values) - 1):
        lo, hi = values[i], values[i + 1]
        if lo <= 0.0 <= hi:
            if hi == lo:
                cdf = levels[i]
            else:
                frac = (0.0 - lo) / (hi - lo)
                cdf = levels[i] + frac * (levels[i + 1] - levels[i])
            return round(1.0 - cdf, 4)
    return None


def _m(node: Any, key: str) -> dict[str, Any]:
    """The child mapping at ``key`` as a plain dict (or ``{}``)."""
    value = node.get(key) if isinstance(node, Mapping) else None
    return dict(value) if isinstance(value, Mapping) else {}


def _base_rate_blocks(packet: Mapping[str, Any]) -> dict[str, Any]:
    return _m(_m(packet, "base_rates"), "by_horizon")


def _implied_blocks(packet: Mapping[str, Any]) -> dict[str, Any]:
    return _m(_m(packet, "implied"), "by_horizon")


def _stack_blocks(packet: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
    stack = packet.get("stack")
    if not isinstance(stack, Mapping) or not stack.get("published"):
        return False, {}
    return True, _m(stack, "by_horizon")


def build_odds(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Build ``packet["odds"]`` from base_rates, implied and (optionally) stack.

    Returns a dict keyed by the string horizon, each carrying ``source``,
    ``quantiles``, ``p_up``, ``base_rate_q50`` and ``shrink_w``. Horizons with no
    usable source are present with ``quantiles = None`` and a ``reason``.
    """
    base_by_h = _base_rate_blocks(packet)
    implied_by_h = _implied_blocks(packet)
    stack_published, stack_by_h = _stack_blocks(packet)

    by_horizon: dict[str, Any] = {}
    for h in HORIZONS:
        key = str(h)
        br_block = _m(base_by_h, key)
        shrunk = _m(br_block, "shrunk")
        uncond = _m(br_block, "uncond")

        br_q = _quantile_map(shrunk) or _quantile_map(uncond)
        base_rate_q50 = _finite(shrunk.get("q50"))
        if base_rate_q50 is None:
            base_rate_q50 = _finite(uncond.get("q50"))
        shrink_w = _finite(shrunk.get("w"))

        imp_block = _m(implied_by_h, key)
        imp_q = _quantile_map(imp_block.get("rw_quantiles")) or _quantile_map(
            imp_block.get("quantiles")
        )

        # Industry median lifts the stack's *excess* quantiles to total return.
        ind_shrunk = _m(_m(br_block, "industry"), "shrunk")
        industry_median = _finite(ind_shrunk.get("q50"))

        stack_block = _m(stack_by_h, key) if stack_published else {}
        stack_q = None
        raw = _quantile_map(stack_block.get("quantiles"))
        if raw is not None and stack_block.get("passed_gates"):
            lift = industry_median if industry_median is not None else 0.0
            stack_q = {k: round(raw[k] + lift, 6) for k in QUANTILE_KEYS}

        quantiles: dict[str, float] | None
        source: str | None
        reason: str | None = None
        if stack_q is not None:
            quantiles, source = stack_q, "stack"
        elif br_q is not None and imp_q is not None:
            quantiles, source = _blend(br_q, imp_q), "base_rates+implied"
        elif br_q is not None:
            quantiles, source = br_q, "base_rates"
        elif imp_q is not None:
            quantiles, source = imp_q, "implied"
        else:
            quantiles, source = None, None
            reason = "no base-rate or implied distribution for this horizon"

        entry: dict[str, Any] = {
            "source": source,
            "quantiles": quantiles,
            "p_up": _p_up_from_quantiles(quantiles) if quantiles else None,
            "base_rate_q50": base_rate_q50,
            "shrink_w": shrink_w,
            "implied_q50": _finite((imp_q or {}).get("q50")) if imp_q else None,
        }
        if reason:
            entry["reason"] = reason
        by_horizon[key] = entry

    return {
        "version": ODDS_VERSION,
        "method": "stack_or_base_rates_plus_implied",
        "stack_published": bool(stack_published),
        "by_horizon": by_horizon,
    }


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------


def top_exposure_drivers(packet: Mapping[str, Any], *, limit: int = 2) -> list[dict[str, Any]]:
    """The ``limit`` largest-magnitude basket betas, most influential first."""
    exposure = packet.get("exposure")
    if not isinstance(exposure, Mapping):
        return []
    betas = exposure.get("betas")
    if not isinstance(betas, Mapping):
        return []
    scored: list[tuple[float, str, float]] = []
    for name, beta in betas.items():
        value = _finite(beta)
        if value is None:
            continue
        scored.append((abs(value), str(name), value))
    scored.sort(key=lambda row: row[0], reverse=True)
    drivers: list[dict[str, Any]] = []
    for _magnitude, name, value in scored[: max(0, int(limit))]:
        drivers.append(
            {
                "name": name,
                "beta": round(value, 4),
                "direction": "positive" if value >= 0 else "negative",
            }
        )
    return drivers


def build_scenarios(
    packet: Mapping[str, Any], odds: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Build ``packet["scenarios"]`` (SPEC §6.6).

    Each of bull / neutral / bear is defined by a *state*, the corresponding
    quantile of the odds distribution at 3/6/12m, and the top-two exposure
    drivers. Reads ``odds`` (passed in or from the packet) so the scenario
    numbers are exactly the distribution the memo quotes — no separate model.
    """
    if odds is None:
        odds = packet.get("odds") if isinstance(packet.get("odds"), Mapping) else {}
    by_horizon = (odds or {}).get("by_horizon") if isinstance(odds, Mapping) else {}
    by_horizon = by_horizon if isinstance(by_horizon, Mapping) else {}
    drivers = top_exposure_drivers(packet, limit=2)

    scenarios: dict[str, Any] = {
        "version": ODDS_VERSION,
        "driver_basis": "largest |beta| basket legs",
    }
    for name, quantile_key in _SCENARIO_QUANTILE.items():
        horizons: dict[str, Any] = {}
        for h in SCENARIO_HORIZONS:
            block = by_horizon.get(str(h)) if isinstance(by_horizon.get(str(h)), Mapping) else {}
            quantiles = block.get("quantiles") if isinstance(block, Mapping) else None
            value = (
                _finite((quantiles or {}).get(quantile_key))
                if isinstance(quantiles, Mapping)
                else None
            )
            horizons[str(h)] = {
                "quantile": value,
                "quantile_key": quantile_key,
                "source": (block or {}).get("source"),
                "drivers": drivers,
            }
        scenarios[name] = {
            "state": _SCENARIO_STATE[name],
            "quantile_key": quantile_key,
            "horizons": horizons,
        }
    return scenarios
