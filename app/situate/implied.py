"""Option-implied forward-return distribution (SPEC 5.4).

The full Massive option-chain snapshot (``/v3/snapshot/options/{T}``) is
paginated in its entirety (following ``next_url``/``next_cursor``), grouped by
expiry, and — for the expiry nearest each horizon in ``{1,2,3,6,12,18}`` months —
turned into a risk-neutral density and its summary statistics:

1. **Own IVs from mids.** Massive's ``implied_volatility``/greeks are often empty,
   so we never read them. Each strike's price is the mid ``(bid + ask) / 2`` when a
   two-sided quote exists, else the day close / last trade. We invert Black-Scholes
   ourselves (out-of-the-money side per strike) to get an IV per strike.
2. **Smile.** A shape-preserving monotone cubic spline (PCHIP, scipy) is fit to
   IV against log-moneyness ``ln(K/F)``. PCHIP does not overshoot, so the
   reconstructed call-price curve stays well behaved.
3. **No-arbitrage checks.** Call prices reconstructed from the smile must be
   non-increasing and convex in the strike; residual butterfly violations are
   clipped to zero density and flagged.
4. **Breeden-Litzenberger.** The risk-neutral density is
   ``f(K) = e^{rT} ∂²C/∂K²`` by central differences on a dense strike grid,
   normalised to a proper PDF.
5. **Statistics.** Return quantiles (5/25/50/75/95), ATM IV, 25-delta skew,
   ``P(±10%)``/``P(±20%)``, and ``width_ratio`` = implied IQR ÷ the historical
   conditional IQR from ``base_rates``. The real-world overlay shifts the density's
   mean to the shrunk base-rate median — a documented heuristic, kept separate
   from the risk-neutral numbers.

Any expiry with fewer than :data:`MIN_USABLE_STRIKES` usable strikes yields
``None`` for that horizon with a reason. IV is never invented.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

import numpy as np

try:
    from app.situate.contract import HORIZONS as _CONTRACT_HORIZONS

    HORIZONS: tuple[int, ...] = tuple(_CONTRACT_HORIZONS)
except Exception:  # noqa: BLE001
    HORIZONS = (1, 2, 3, 6, 12, 18)

IMPLIED_VERSION = "1.0.0"

#: Minimum distinct usable strikes before an expiry produces a density.
MIN_USABLE_STRIKES = 5
#: Days-to-expiry targeted for each horizon (months → calendar days).
HORIZON_TARGET_DAYS: dict[int, int] = {1: 30, 2: 61, 3: 91, 6: 182, 12: 365, 18: 548}
#: Grid resolution for the Breeden-Litzenberger second difference.
_DENSITY_GRID = 400
#: Downsampled density points returned in the packet.
_DENSITY_OUTPUT = 61
_QUANTILE_LEVELS: tuple[float, ...] = (0.05, 0.25, 0.50, 0.75, 0.95)
_QUANTILE_KEYS: tuple[str, ...] = ("q05", "q25", "q50", "q75", "q95")
_DEFAULT_RISK_FREE = 0.04
#: Bounds for the Black-Scholes IV root search.
_IV_LO, _IV_HI = 1e-4, 5.0


# --------------------------------------------------------------------------- #
# Black-Scholes primitives (no scipy.stats dependency for the CDF).
# --------------------------------------------------------------------------- #
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def bs_price(
    spot: float, strike: float, t: float, r: float, sigma: float, *, is_call: bool, q: float = 0.0
) -> float:
    """Black-Scholes European option price (continuous dividend yield ``q``)."""
    if t <= 0.0 or sigma <= 0.0 or spot <= 0.0 or strike <= 0.0:
        forward_intrinsic = spot * math.exp(-q * t) - strike * math.exp(-r * t)
        if is_call:
            return max(0.0, forward_intrinsic)
        return max(0.0, -forward_intrinsic)
    vol_t = sigma * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t) / vol_t
    d2 = d1 - vol_t
    disc_s = spot * math.exp(-q * t)
    disc_k = strike * math.exp(-r * t)
    if is_call:
        return disc_s * _norm_cdf(d1) - disc_k * _norm_cdf(d2)
    return disc_k * _norm_cdf(-d2) - disc_s * _norm_cdf(-d1)


def bs_delta(
    spot: float, strike: float, t: float, r: float, sigma: float, *, is_call: bool, q: float = 0.0
) -> float:
    """Black-Scholes delta."""
    if t <= 0.0 or sigma <= 0.0 or spot <= 0.0 or strike <= 0.0:
        return float("nan")
    vol_t = sigma * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t) / vol_t
    if is_call:
        return math.exp(-q * t) * _norm_cdf(d1)
    return -math.exp(-q * t) * _norm_cdf(-d1)


def implied_vol(
    price: float,
    spot: float,
    strike: float,
    t: float,
    r: float,
    *,
    is_call: bool,
    q: float = 0.0,
) -> float | None:
    """Invert Black-Scholes for the implied volatility of one option price.

    Returns ``None`` when the price is outside the no-arbitrage bounds or the root
    search fails — never a guessed value.
    """
    if price is None or price <= 0.0 or t <= 0.0 or spot <= 0.0 or strike <= 0.0:
        return None
    disc_s = spot * math.exp(-q * t)
    disc_k = strike * math.exp(-r * t)
    lower = max(0.0, disc_s - disc_k) if is_call else max(0.0, disc_k - disc_s)
    upper = disc_s if is_call else disc_k
    if price <= lower + 1e-12 or price >= upper - 1e-12:
        return None
    try:
        from scipy.optimize import brentq
    except Exception:  # noqa: BLE001 - scipy must be present, but degrade cleanly
        return _bisect_iv(price, spot, strike, t, r, is_call=is_call, q=q)

    def objective(sigma: float) -> float:
        return bs_price(spot, strike, t, r, sigma, is_call=is_call, q=q) - price

    try:
        f_lo = objective(_IV_LO)
        f_hi = objective(_IV_HI)
        if f_lo * f_hi > 0.0:
            return None
        return float(brentq(objective, _IV_LO, _IV_HI, maxiter=100, xtol=1e-8))
    except (ValueError, RuntimeError):
        return None


def _bisect_iv(
    price: float,
    spot: float,
    strike: float,
    t: float,
    r: float,
    *,
    is_call: bool,
    q: float = 0.0,
) -> float | None:
    lo, hi = _IV_LO, _IV_HI
    f_lo = bs_price(spot, strike, t, r, lo, is_call=is_call, q=q) - price
    f_hi = bs_price(spot, strike, t, r, hi, is_call=is_call, q=q) - price
    if f_lo * f_hi > 0.0:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        f_mid = bs_price(spot, strike, t, r, mid, is_call=is_call, q=q) - price
        if abs(f_mid) < 1e-9:
            return mid
        if f_lo * f_mid <= 0.0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------- #
# Chain fetch + normalisation.
# --------------------------------------------------------------------------- #
def _contract_mid(contract: Mapping[str, Any]) -> float | None:
    """Mid ``(bid+ask)/2`` when a two-sided quote exists, else day close / last."""
    bid = _finite(contract.get("bid"))
    ask = _finite(contract.get("ask"))
    if bid is not None and ask is not None and ask >= bid > 0.0:
        return 0.5 * (bid + ask)
    for key in ("day_close", "last", "close"):
        value = _finite(contract.get(key))
        if value is not None and value > 0.0:
            return value
    return None


def normalize_raw_contracts(
    results: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], float | None]:
    """Flatten Massive snapshot rows into ``{expiry,strike,type,bid,ask,...}`` dicts.

    Returns the contracts and the underlying spot (from ``underlying_asset``).
    """
    contracts: list[dict[str, Any]] = []
    spot: float | None = None
    for row in results:
        if not isinstance(row, Mapping):
            continue
        underlying = row.get("underlying_asset")
        if isinstance(underlying, Mapping) and spot is None:
            for key in ("price", "value", "last_price"):
                candidate = _finite(underlying.get(key))
                if candidate is not None and candidate > 0.0:
                    spot = candidate
                    break
        raw_details = row.get("details")
        details: Mapping[str, Any] = raw_details if isinstance(raw_details, Mapping) else row
        strike = _finite(details.get("strike_price"))
        contract_type = str(details.get("contract_type") or "").lower()
        expiry = details.get("expiration_date")
        if strike is None or strike <= 0.0 or contract_type not in {"call", "put"} or not expiry:
            continue
        raw_quote = row.get("last_quote")
        quote: Mapping[str, Any] = raw_quote if isinstance(raw_quote, Mapping) else {}
        raw_trade = row.get("last_trade")
        trade: Mapping[str, Any] = raw_trade if isinstance(raw_trade, Mapping) else {}
        raw_day = row.get("day")
        day: Mapping[str, Any] = raw_day if isinstance(raw_day, Mapping) else {}
        contracts.append(
            {
                "expiry": str(expiry),
                "strike": float(strike),
                "type": contract_type,
                "bid": _finite(quote.get("bid_price") or quote.get("bp")),
                "ask": _finite(quote.get("ask_price") or quote.get("ap")),
                "last": _finite(trade.get("price") or trade.get("p")),
                "day_close": _finite(day.get("close") or day.get("c")),
                "open_interest": _finite(row.get("open_interest")) or 0.0,
            }
        )
    return contracts, spot


def fetch_full_chain(client: Any, ticker: str) -> tuple[list[dict[str, Any]], float | None]:
    """Paginate the whole option-chain snapshot for ``ticker``.

    Reuses the Massive provider's ``_paginate`` (which follows ``next_url``) and
    walks its ``next_cursor`` so the *entire* chain is retrieved, not just the
    provider's per-call page cap. Returns normalised contracts and the spot.
    """
    provider = getattr(client, "provider", None)
    if provider is None or not hasattr(provider, "_paginate"):
        raise RuntimeError("market client does not expose a paginating options provider")

    path = f"/v3/snapshot/options/{ticker}"
    results: list[Mapping[str, Any]] = []
    params: dict[str, Any] = {"limit": 250}
    seen_cursors: set[str] = set()
    for _ in range(200):  # hard safety cap on cursor hops
        payload = provider._paginate(path, params=dict(params))
        page = payload.get("results")
        if isinstance(page, list):
            results.extend(item for item in page if isinstance(item, Mapping))
        cursor = payload.get("next_cursor")
        if not cursor or cursor in seen_cursors:
            break
        seen_cursors.add(str(cursor))
        params = {"limit": 250, "cursor": cursor}
    return normalize_raw_contracts(results)


def group_by_expiry(contracts: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for contract in contracts:
        expiry = str(contract.get("expiry") or "")
        if not expiry:
            continue
        grouped.setdefault(expiry, []).append(dict(contract))
    return grouped


def pick_expiries(
    expiries: Sequence[str], as_of: date, *, targets: Mapping[int, int] = HORIZON_TARGET_DAYS
) -> dict[int, str]:
    """Nearest expiry (by days-to-expiry) to each horizon target; must be > as_of."""
    parsed: list[tuple[str, int]] = []
    for raw in expiries:
        try:
            dte = (date.fromisoformat(str(raw)) - as_of).days
        except ValueError:
            continue
        if dte > 0:
            parsed.append((str(raw), dte))
    chosen: dict[int, str] = {}
    for horizon, target in targets.items():
        if not parsed:
            continue
        best = min(parsed, key=lambda item: abs(item[1] - target))
        chosen[horizon] = best[0]
    return chosen


# --------------------------------------------------------------------------- #
# Smile fit + Breeden-Litzenberger density.
# --------------------------------------------------------------------------- #
#: Log-moneyness band kept around the forward, to drop unreliable deep wings.
_MAX_ABS_LOG_MONEYNESS = 1.2
#: Minimum option price trusted for an IV inversion (penny options are noise).
_MIN_OPTION_PRICE = 0.05
#: How many robust standard deviations from the median IV a strike may sit.
_IV_OUTLIER_SIGMAS = 6.0
#: Absolute IV floor for the outlier gate so a near-flat smile is not decimated.
_IV_OUTLIER_FLOOR = 0.05


def _reject_iv_outliers(strikes: np.ndarray, ivs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Drop strikes whose inverted IV is a robust (MAD) outlier of the smile.

    Massive's per-contract quotes are frequently empty here, so IVs come from stale
    day-close mids; a handful of wing strikes invert to wild IVs that would wreck
    the smile. Median/MAD rejection removes those without touching the body. A
    (near-)flat smile has ``MAD ≈ 0``; the absolute floor keeps it intact.
    """
    if ivs.size < 4:
        return strikes, ivs
    median = float(np.median(ivs))
    mad = float(np.median(np.abs(ivs - median)))
    threshold = max(_IV_OUTLIER_SIGMAS * 1.4826 * mad, _IV_OUTLIER_FLOOR)
    keep = np.abs(ivs - median) <= threshold
    if int(keep.sum()) < 4:
        return strikes, ivs
    return strikes[keep], ivs[keep]


def _strike_ivs(
    contracts: Sequence[Mapping[str, Any]], spot: float, t: float, r: float, forward: float
) -> tuple[np.ndarray, np.ndarray]:
    """IV per strike from the out-of-the-money side, sorted by strike.

    OTM puts (``K ≤ F``) and OTM calls (``K > F``) carry the cleaner information,
    so each strike is inverted from whichever side is OTM relative to the forward.
    Mids are ``(bid+ask)/2`` when a quote exists, else the day close / last trade
    (:func:`_contract_mid`). Strikes outside :data:`_MAX_ABS_LOG_MONEYNESS`, priced
    below :data:`_MIN_OPTION_PRICE`, or whose IV is a robust outlier are dropped.
    """
    by_strike: dict[float, dict[str, Mapping[str, Any]]] = {}
    for contract in contracts:
        strike = _finite(contract.get("strike"))
        kind = str(contract.get("type") or "").lower()
        if strike is None or strike <= 0.0 or kind not in {"call", "put"}:
            continue
        if abs(math.log(strike / forward)) > _MAX_ABS_LOG_MONEYNESS:
            continue
        by_strike.setdefault(strike, {})[kind] = contract

    strikes: list[float] = []
    ivs: list[float] = []
    for strike in sorted(by_strike):
        sides = by_strike[strike]
        is_call = strike > forward
        primary = sides.get("call" if is_call else "put")
        fallback = sides.get("put" if is_call else "call")
        iv: float | None = None
        if primary is not None:
            mid = _contract_mid(primary)
            if mid is not None and mid >= _MIN_OPTION_PRICE:
                iv = implied_vol(mid, spot, strike, t, r, is_call=is_call)
        if iv is None and fallback is not None:
            mid = _contract_mid(fallback)
            if mid is not None and mid >= _MIN_OPTION_PRICE:
                iv = implied_vol(mid, spot, strike, t, r, is_call=(not is_call))
        if iv is not None and _IV_LO < iv < _IV_HI:
            strikes.append(strike)
            ivs.append(iv)
    return _reject_iv_outliers(np.asarray(strikes, dtype=float), np.asarray(ivs, dtype=float))


def _pava_increasing(values: np.ndarray) -> np.ndarray:
    """Isotonic (non-decreasing) regression by pool-adjacent-violators.

    Used to project the raw Breeden-Litzenberger CDF onto the monotone
    non-decreasing functions — the least-squares way to remove the small
    non-monotonicities that market-quote noise leaves in ``1 + e^{rT} ∂C/∂K``,
    which guarantees a non-negative density without the bias of a cumulative max.
    """
    y = np.asarray(values, dtype=float)
    level_vals: list[float] = []
    level_cnts: list[int] = []
    for value in y:
        v = float(value)
        c = 1
        while level_vals and level_vals[-1] > v:
            prev_v = level_vals.pop()
            prev_c = level_cnts.pop()
            v = (v * c + prev_v * prev_c) / (c + prev_c)
            c += prev_c
        level_vals.append(v)
        level_cnts.append(c)
    out = np.empty(y.size, dtype=float)
    idx = 0
    for v, c in zip(level_vals, level_cnts, strict=True):
        out[idx : idx + c] = v
        idx += c
    return out


def _quantiles_from_cdf(grid: np.ndarray, cdf: np.ndarray, levels: Sequence[float]) -> list[float]:
    return [float(np.interp(level, cdf, grid)) for level in levels]


def fit_density(
    contracts: Sequence[Mapping[str, Any]],
    *,
    spot: float,
    t: float,
    r: float,
    q: float = 0.0,
    min_strikes: int = MIN_USABLE_STRIKES,
) -> dict[str, Any] | None:
    """Fit the smile and return the risk-neutral density + statistics, or ``None``.

    ``None`` (with the caller recording a reason) is returned when fewer than
    ``min_strikes`` usable strikes survive inversion or the density is degenerate.
    """
    if spot <= 0.0 or t <= 0.0:
        return None
    forward = spot * math.exp((r - q) * t)
    strikes, ivs = _strike_ivs(contracts, spot, t, r, forward)
    if strikes.size < min_strikes:
        return None

    log_moneyness = np.log(strikes / forward)
    order = np.argsort(log_moneyness)
    log_moneyness, ivs_sorted, strikes_sorted = (
        log_moneyness[order],
        ivs[order],
        strikes[order],
    )
    # Collapse duplicate log-moneyness points (PCHIP needs strictly increasing x).
    unique_k, unique_idx = np.unique(log_moneyness, return_index=True)
    if unique_k.size < min_strikes:
        return None
    unique_iv = ivs_sorted[unique_idx]
    _ = strikes_sorted  # ordering already captured via unique_k

    # Volatility-scaled moneyness band: keep strikes within ~4 ATM-vol standard
    # deviations of the forward. This is tight for short expiries (where stale
    # far-OTM day-close mids otherwise inject fake tail mass) and wide for long
    # ones. The band only tightens the chain when doing so still leaves a healthy
    # body; on a thin chain it widens (to 0.35, then unbounded) rather than starve.
    atm_guess = float(np.interp(0.0, unique_k, unique_iv))
    vol_band = 3.0 * atm_guess * math.sqrt(t)
    healthy = max(2 * min_strikes, 10)
    for band in (vol_band, 0.35):
        band_mask = np.abs(unique_k) <= band
        if int(band_mask.sum()) >= healthy:
            unique_k, unique_iv = unique_k[band_mask], unique_iv[band_mask]
            break

    try:
        from scipy.interpolate import PchipInterpolator
    except Exception:  # noqa: BLE001
        return None
    smile = PchipInterpolator(unique_k, unique_iv, extrapolate=False)

    # Dense strike grid across the retained log-moneyness range (no extrapolation).
    k_lo = float(forward * math.exp(unique_k[0]))
    k_hi = float(forward * math.exp(unique_k[-1]))
    grid = np.linspace(k_lo, k_hi, _DENSITY_GRID)
    grid_log_m = np.log(grid / forward)
    iv_grid = smile(grid_log_m)
    # PCHIP returns NaN outside the fitted range; clamp to the nearest edge IV.
    iv_grid = np.where(np.isnan(iv_grid), np.interp(grid_log_m, unique_k, unique_iv), iv_grid)
    iv_grid = np.clip(iv_grid, _IV_LO, _IV_HI)

    call_prices = np.asarray(
        [
            bs_price(spot, float(k), t, r, float(v), is_call=True, q=q)
            for k, v in zip(grid, iv_grid, strict=True)
        ]
    )

    # Breeden-Litzenberger via the CDF, which is far more robust to quote noise
    # than differentiating the call curve twice: the risk-neutral CDF is
    #   F(K) = 1 + e^{rT} ∂C/∂K,   monotone non-decreasing from 0 to 1.
    # We take the first difference, form the raw CDF, and project it onto the
    # monotone non-decreasing functions (isotonic regression) so the density
    # d/dK F(K) is non-negative by construction — no clip-away of arbitrage.
    disc = math.exp(r * t)
    mid_k = 0.5 * (grid[1:] + grid[:-1])
    dcall_dk = np.diff(call_prices) / np.diff(grid)
    raw_cdf = 1.0 + disc * dcall_dk

    # Honest chain-quality diagnostics from the RAW (pre-repair) curve.
    monotone_ok = bool(np.all(dcall_dk <= 1e-9))
    convex_violation = float(np.mean(np.diff(raw_cdf) < -1e-9))

    cdf = _pava_increasing(np.clip(raw_cdf, 0.0, 1.0))
    span = float(cdf[-1] - cdf[0])
    if not math.isfinite(span) or span < 0.5:
        # The observed strikes do not bracket enough probability mass to trust.
        return None

    # Renormalise to unit mass over the observed strike range so the density, the
    # quantiles and the tail probabilities all describe the SAME distribution. The
    # fitted density is truncated to observed strikes; on long-dated chains the raw
    # CDF spans only ~0.83, and reading quantiles off it both clamps the tails to
    # the boundary strikes and disagrees with the (unit-mass) serialized density.
    # ``span`` is retained above as the honest chain-coverage diagnostic.
    cdf = (cdf - cdf[0]) / span

    density = np.clip(np.diff(cdf) / np.diff(mid_k), 0.0, None)
    dens_k = 0.5 * (mid_k[1:] + mid_k[:-1])
    area = float(np.trapezoid(density, dens_k))
    if not math.isfinite(area) or area <= 0.0:
        return None
    density = density / area

    price_quantiles = _quantiles_from_cdf(mid_k, cdf, _QUANTILE_LEVELS)
    return_quantiles = {
        key: value / spot - 1.0 for key, value in zip(_QUANTILE_KEYS, price_quantiles, strict=True)
    }
    # Risk-neutral mean return is the forward return by no-arbitrage; the density
    # is truncated to the observed strikes, so the theoretical value is the stable
    # anchor for the base-rate mean-shift heuristic.
    rn_mean_return = forward / spot - 1.0

    def prob_above(price_level: float) -> float:
        return float(1.0 - np.interp(price_level, mid_k, cdf))

    atm_iv = (
        _finite(float(smile(0.0)))
        if (k_lo <= forward <= k_hi)
        else _finite(float(np.interp(0.0, unique_k, unique_iv)))
    )
    skew = _delta_skew(unique_k, unique_iv, spot, forward, t, r, q)

    # Serialize the density mass-preservingly instead of point-sampling it. A
    # coarse ``np.interp`` onto a 61-point linspace aliases wide/spiky long-dated
    # densities: it can drop a large fraction of the mass and latch onto a
    # peak-strike, so the published density then fails the "integrates to ~1"
    # criterion and contradicts the packet's own (full-resolution) quantiles.
    # Integrate the full-resolution PAVA CDF into each output cell so total mass
    # and shape survive the down-sampling, then renormalize to unit area.
    output_grid = np.linspace(dens_k[0], dens_k[-1], _DENSITY_OUTPUT)
    edges = np.empty(_DENSITY_OUTPUT + 1)
    edges[1:-1] = 0.5 * (output_grid[1:] + output_grid[:-1])
    edges[0] = output_grid[0]
    edges[-1] = output_grid[-1]
    cdf_at_edges = np.interp(edges, mid_k, cdf)
    cell_widths = np.diff(edges)
    output_pdf = np.where(cell_widths > 0.0, np.diff(cdf_at_edges) / cell_widths, 0.0)
    output_pdf = np.clip(output_pdf, 0.0, None)
    output_area = float(np.trapezoid(output_pdf, output_grid))
    if math.isfinite(output_area) and output_area > 0.0:
        output_pdf = output_pdf / output_area
    rn_density = [
        {"k": round(float(k), 4), "pdf": round(float(p), 8)}
        for k, p in zip(output_grid, output_pdf, strict=True)
    ]

    return {
        "quantiles": {k: round(v, 6) for k, v in return_quantiles.items()},
        "iv_atm": round(atm_iv, 6) if atm_iv is not None else None,
        "skew_25d": skew,
        "p_up10": round(prob_above(spot * 1.10), 6),
        "p_dn10": round(1.0 - prob_above(spot * 0.90), 6),
        "p_up20": round(prob_above(spot * 1.20), 6),
        "p_dn20": round(1.0 - prob_above(spot * 0.80), 6),
        "rn_mean_return": round(rn_mean_return, 6),
        "cdf_span": round(span, 4),
        "rn_density": rn_density,
        "n_strikes": int(unique_k.size),
        "forward": round(forward, 6),
        "monotone_ok": monotone_ok,
        "convex_violation_frac": round(convex_violation, 4),
        "quality": "ok" if convex_violation <= 0.25 else "noisy_tails",
        "strike_range": [round(k_lo, 4), round(k_hi, 4)],
    }


def _delta_skew(
    log_moneyness: np.ndarray,
    ivs: np.ndarray,
    spot: float,
    forward: float,
    t: float,
    r: float,
    q: float,
) -> float | None:
    """25-delta skew = IV(25Δ put) − IV(25Δ call) from the fitted smile."""
    try:
        from scipy.interpolate import PchipInterpolator
    except Exception:  # noqa: BLE001
        return None
    smile = PchipInterpolator(log_moneyness, ivs, extrapolate=True)
    strikes = forward * np.exp(np.linspace(log_moneyness[0], log_moneyness[-1], 200))
    call_delta: list[tuple[float, float]] = []
    put_delta: list[tuple[float, float]] = []
    for strike in strikes:
        iv = float(smile(math.log(strike / forward)))
        if not (_IV_LO < iv < _IV_HI):
            continue
        cd = bs_delta(spot, float(strike), t, r, iv, is_call=True, q=q)
        pd_ = bs_delta(spot, float(strike), t, r, iv, is_call=False, q=q)
        if math.isfinite(cd):
            call_delta.append((abs(cd - 0.25), iv))
        if math.isfinite(pd_):
            put_delta.append((abs(abs(pd_) - 0.25), iv))
    if not call_delta or not put_delta:
        return None
    iv_call25 = min(call_delta, key=lambda item: item[0])[1]
    iv_put25 = min(put_delta, key=lambda item: item[0])[1]
    return round(iv_put25 - iv_call25, 6)


# --------------------------------------------------------------------------- #
# Orchestration.
# --------------------------------------------------------------------------- #
def build_implied(
    client: Any,
    ticker: str,
    *,
    as_of: date | str | None = None,
    spot: float | None = None,
    horizons: tuple[int, ...] = HORIZONS,
    risk_free_annual: float = _DEFAULT_RISK_FREE,
    dividend_yield: float = 0.0,
    hist_cond_iqr: Mapping[int, float | None] | None = None,
    shrunk_base_median: Mapping[int, float | None] | None = None,
    min_strikes: int = MIN_USABLE_STRIKES,
) -> dict[str, Any]:
    """Build ``packet["implied"]`` (SPEC 5.4).

    ``hist_cond_iqr`` and ``shrunk_base_median`` come from ``base_rates`` (per
    horizon, keyed by integer months). ``client`` must expose a Massive provider
    with ``_paginate``; when it does not, or the chain is empty, every horizon is
    ``None`` and the reason is recorded in ``errors``.
    """
    resolved_as_of = _resolve_date(as_of)
    section: dict[str, Any] = {
        "snapshot_ts": datetime.now(UTC).isoformat(),
        "provider": "massive",
        "risk_free_annual": risk_free_annual,
        "dividend_yield": dividend_yield,
        "underlying_price": None,
        "by_horizon": {str(h): None for h in horizons},
        "errors": [],
        "unavailable": [],
    }

    try:
        contracts, chain_spot = fetch_full_chain(client, ticker)
    except Exception as exc:  # noqa: BLE001 - a missing entitlement is data, not a crash
        section["errors"].append({"source": "implied.chain", "error": str(exc)})
        for h in horizons:
            section["unavailable"].append(
                {"horizon": h, "reason": f"option chain unavailable: {exc}"}
            )
        return section

    resolved_spot = _finite(spot) or chain_spot
    section["underlying_price"] = resolved_spot
    if resolved_spot is None or resolved_spot <= 0.0:
        section["errors"].append(
            {"source": "implied.spot", "error": "no underlying price in chain"}
        )
        for h in horizons:
            section["unavailable"].append({"horizon": h, "reason": "no underlying price"})
        return section
    if not contracts:
        for h in horizons:
            section["unavailable"].append({"horizon": h, "reason": "empty option chain"})
        return section

    grouped = group_by_expiry(contracts)
    section["n_expiries"] = len(grouped)
    section["n_contracts"] = len(contracts)
    chosen = pick_expiries(sorted(grouped), resolved_as_of)

    for h in horizons:
        expiry = chosen.get(h)
        if expiry is None:
            section["unavailable"].append({"horizon": h, "reason": "no expiry near this horizon"})
            continue
        t_years = max((date.fromisoformat(expiry) - resolved_as_of).days, 1) / 365.25
        try:
            density = fit_density(
                grouped[expiry],
                spot=resolved_spot,
                t=t_years,
                r=risk_free_annual,
                q=dividend_yield,
                min_strikes=min_strikes,
            )
        except Exception as exc:  # noqa: BLE001 - one thin expiry cannot sink the rest
            density = None
            section["errors"].append({"source": f"implied.{h}m", "error": str(exc)})
        if density is None:
            section["unavailable"].append(
                {
                    "horizon": h,
                    "expiry": expiry,
                    "reason": f"fewer than {min_strikes} usable strikes",
                }
            )
            continue

        block = dict(density)
        block["expiry"] = expiry
        block["t_years"] = round(t_years, 4)
        block["horizon_months"] = h

        implied_iqr = _finite(block["quantiles"]["q75"]) - _finite(  # type: ignore[operator]
            block["quantiles"]["q25"]
        )
        hist_iqr = _finite((hist_cond_iqr or {}).get(h))
        block["implied_iqr"] = round(implied_iqr, 6) if implied_iqr is not None else None
        block["width_ratio_vs_hist"] = (
            round(implied_iqr / hist_iqr, 4) if (hist_iqr is not None and hist_iqr > 0) else None
        )

        target_median = _finite((shrunk_base_median or {}).get(h))
        block["rw_quantiles"], block["mean_shift"] = _shift_to_base_rate(
            block["quantiles"], rn_mean=block["rn_mean_return"], target_median=target_median
        )
        section["by_horizon"][str(h)] = block

    return section


def _shift_to_base_rate(
    rn_quantiles: Mapping[str, Any], *, rn_mean: float | None, target_median: float | None
) -> tuple[dict[str, float | None] | None, float | None]:
    """Real-world overlay: shift the RN density's mean to the base-rate median.

    Documented heuristic (SPEC 5.4): the risk-neutral density is what the market
    is pricing; to read it as a physical-measure estimate we shift its mean to the
    shrunk base-rate median. Kept separate from the risk-neutral quantiles so the
    "what's priced in" numbers are never contaminated.
    """
    if target_median is None or rn_mean is None:
        return None, None
    shift = target_median - rn_mean
    shifted: dict[str, float | None] = {}
    for key in _QUANTILE_KEYS:
        value = _finite(rn_quantiles.get(key))
        shifted[key] = round(value + shift, 6) if value is not None else None
    return shifted, round(shift, 6)


def _resolve_date(as_of: date | str | None) -> date:
    if as_of is None:
        return datetime.now(UTC).date()
    if isinstance(as_of, date):
        return as_of
    return date.fromisoformat(str(as_of))
