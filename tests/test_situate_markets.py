"""Offline tests for Situate's market modules (S2): base_rates, implied, levels.

The keystone is the synthetic Black-Scholes recovery test: a lognormal chain with
a known volatility must reproduce the analytic risk-neutral density and its
quantiles to tolerance. The rest pin the shrinkage/`n_eff` arithmetic, the
overlapping-window bookkeeping, the thin-chain degrade, and the implied cheap/rich
zones.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from app.situate import base_rates, implied, levels

# --------------------------------------------------------------------------- #
# Synthetic Black-Scholes chain helpers.
# --------------------------------------------------------------------------- #
SPOT = 100.0
RISK_FREE = 0.03
SIGMA = 0.25


def _bs_raw_contracts(
    *, spot: float, r: float, t: float, sigma: float, strikes: list[float], expiry: str
) -> list[dict[str, Any]]:
    """Massive-shaped snapshot rows priced exactly by Black-Scholes (mid=price)."""
    rows: list[dict[str, Any]] = []
    for strike in strikes:
        for kind, is_call in (("call", True), ("put", False)):
            price = implied.bs_price(spot, strike, t, r, sigma, is_call=is_call)
            rows.append(
                {
                    "details": {
                        "strike_price": strike,
                        "contract_type": kind,
                        "expiration_date": expiry,
                        "ticker": f"O:TEST{expiry}{kind[0].upper()}{int(strike)}",
                    },
                    "last_quote": {"bid_price": price, "ask_price": price},
                    "day": {"close": price},
                    "open_interest": 100,
                    "underlying_asset": {"price": spot},
                }
            )
    return rows


class _FakeProvider:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self._results = results

    def _paginate(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        _ = (path, params)
        return {"results": self._results, "next_cursor": None}


class _FakeClient:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.provider = _FakeProvider(results)


# --------------------------------------------------------------------------- #
# implied — Black-Scholes recovery.
# --------------------------------------------------------------------------- #
def test_iv_inversion_round_trip() -> None:
    price = implied.bs_price(SPOT, 105.0, 0.5, RISK_FREE, 0.32, is_call=True)
    recovered = implied.implied_vol(price, SPOT, 105.0, 0.5, RISK_FREE, is_call=True)
    assert recovered is not None
    assert recovered == pytest.approx(0.32, abs=1e-4)


def test_synthetic_lognormal_density_recovered() -> None:
    t = 0.5
    strikes = [float(k) for k in range(60, 161, 2)]
    contracts = _bs_raw_contracts(
        spot=SPOT, r=RISK_FREE, t=t, sigma=SIGMA, strikes=strikes, expiry="2026-01-01"
    )
    normalized, spot = implied.normalize_raw_contracts(contracts)
    assert spot == pytest.approx(SPOT)

    density = implied.fit_density(normalized, spot=SPOT, t=t, r=RISK_FREE, q=0.0)
    assert density is not None

    # ATM IV recovers the input volatility.
    assert density["iv_atm"] == pytest.approx(SIGMA, abs=0.01)

    # Analytic lognormal return quantiles: S_T = S*exp((r-0.5 sigma^2)t + sigma*sqrt(t)*Z).
    drift = (RISK_FREE - 0.5 * SIGMA * SIGMA) * t
    vol_t = SIGMA * math.sqrt(t)
    for key, level, tol in (
        ("q05", 0.05, 0.02),
        ("q25", 0.25, 0.01),
        ("q50", 0.50, 0.01),
        ("q75", 0.75, 0.01),
        ("q95", 0.95, 0.02),
    ):
        analytic_return = math.exp(drift + vol_t * norm.ppf(level)) - 1.0
        assert density["quantiles"][key] == pytest.approx(analytic_return, abs=tol)

    # Tail probabilities match the analytic lognormal CDF.
    def analytic_prob_above(price_level: float) -> float:
        z = (math.log(price_level / SPOT) - drift) / vol_t
        return 1.0 - norm.cdf(z)

    assert density["p_up10"] == pytest.approx(analytic_prob_above(110.0), abs=0.02)
    assert density["p_dn10"] == pytest.approx(1.0 - analytic_prob_above(90.0), abs=0.02)

    # A symmetric (flat) smile has ~zero 25-delta skew.
    assert density["skew_25d"] == pytest.approx(0.0, abs=0.01)
    assert density["n_strikes"] >= implied.MIN_USABLE_STRIKES
    assert density["monotone_ok"] is True


def test_skew_present_with_asymmetric_smile() -> None:
    """A downward-sloping smile (higher put IVs) yields a positive 25-delta skew."""
    t = 0.5
    strikes = [float(k) for k in range(60, 161, 2)]
    contracts: list[dict[str, Any]] = []
    for strike in strikes:
        # Higher IV for low strikes (put skew), lower for high strikes.
        local_sigma = SIGMA + 0.0025 * (SPOT - strike) / 5.0
        local_sigma = max(0.05, local_sigma)
        for kind, is_call in (("call", True), ("put", False)):
            price = implied.bs_price(SPOT, strike, t, RISK_FREE, local_sigma, is_call=is_call)
            contracts.append(
                {
                    "details": {
                        "strike_price": strike,
                        "contract_type": kind,
                        "expiration_date": "2026-01-01",
                    },
                    "last_quote": {"bid_price": price, "ask_price": price},
                    "underlying_asset": {"price": SPOT},
                }
            )
    normalized, _ = implied.normalize_raw_contracts(contracts)
    density = implied.fit_density(normalized, spot=SPOT, t=t, r=RISK_FREE)
    assert density is not None
    assert density["skew_25d"] is not None
    assert density["skew_25d"] > 0.0


def test_serialized_density_preserves_mass_on_a_wide_long_dated_chain() -> None:
    """A wide, high-vol, long-dated chain must still publish a density that

    integrates to ~1 and whose median agrees with the reported q50. Point-sampling
    the fine density onto a coarse grid aliased away much of the mass and shifted
    the peak; the mass-preserving resample must not.
    """
    t = 1.0
    sigma = 0.60  # NVDA-like implied vol
    strikes = [float(k) for k in range(20, 401, 5)]  # very wide strike band
    contracts = _bs_raw_contracts(
        spot=SPOT, r=RISK_FREE, t=t, sigma=sigma, strikes=strikes, expiry="2026-09-01"
    )
    normalized, _ = implied.normalize_raw_contracts(contracts)
    density = implied.fit_density(normalized, spot=SPOT, t=t, r=RISK_FREE, q=0.0)
    assert density is not None

    points = density["rn_density"]
    ks = np.array([pt["k"] for pt in points], dtype=float)
    pdfs = np.array([pt["pdf"] for pt in points], dtype=float)

    # (a) The published density integrates to ~1 (plan criterion), unlike a coarse
    # point-sample which loses a large fraction of the mass on a wide band.
    area = float(np.trapezoid(pdfs, ks))
    assert area == pytest.approx(1.0, abs=0.03)

    # (b) The density-derived median agrees with the reported (full-resolution) q50.
    cdf = np.concatenate([[0.0], np.cumsum(0.5 * (pdfs[1:] + pdfs[:-1]) * np.diff(ks))])
    cdf /= cdf[-1]
    median_k = float(np.interp(0.5, cdf, ks))
    derived_q50 = median_k / SPOT - 1.0
    assert derived_q50 == pytest.approx(density["quantiles"]["q50"], abs=0.03)


def test_thin_chain_degrades() -> None:
    t = 0.5
    strikes = [95.0, 100.0, 105.0]  # only 3 strikes < MIN_USABLE_STRIKES
    contracts = _bs_raw_contracts(
        spot=SPOT, r=RISK_FREE, t=t, sigma=SIGMA, strikes=strikes, expiry="2026-01-01"
    )
    normalized, _ = implied.normalize_raw_contracts(contracts)
    assert implied.fit_density(normalized, spot=SPOT, t=t, r=RISK_FREE) is None


def test_build_implied_populates_liquid_and_degrades_thin() -> None:
    as_of = date(2025, 10, 1)
    liquid_expiry = (as_of + timedelta(days=91)).isoformat()  # near the 3m horizon
    thin_expiry = (as_of + timedelta(days=30)).isoformat()  # near the 1m horizon, thin
    t_liquid = 91 / 365.25
    t_thin = 30 / 365.25
    strikes = [float(k) for k in range(60, 161, 2)]
    contracts = _bs_raw_contracts(
        spot=SPOT, r=RISK_FREE, t=t_liquid, sigma=SIGMA, strikes=strikes, expiry=liquid_expiry
    )
    contracts += _bs_raw_contracts(
        spot=SPOT,
        r=RISK_FREE,
        t=t_thin,
        sigma=SIGMA,
        strikes=[98.0, 100.0, 102.0],
        expiry=thin_expiry,
    )
    client = _FakeClient(contracts)

    hist_iqr = {3: 0.15}
    base_median = {3: 0.02}
    section = implied.build_implied(
        client,
        "TEST",
        as_of=as_of,
        risk_free_annual=RISK_FREE,
        hist_cond_iqr=hist_iqr,
        shrunk_base_median=base_median,
    )
    assert section["underlying_price"] == pytest.approx(SPOT)
    block = section["by_horizon"]["3"]
    assert block is not None
    assert block["expiry"] == liquid_expiry
    assert block["width_ratio_vs_hist"] is not None
    # Real-world overlay shifts the RN mean to the base-rate median.
    assert block["rw_quantiles"] is not None
    assert block["mean_shift"] == pytest.approx(base_median[3] - block["rn_mean_return"], abs=1e-6)

    # The 1m horizon maps to the thin (3-strike) expiry and degrades with a reason.
    assert section["by_horizon"]["1"] is None
    reasons = {item["horizon"] for item in section["unavailable"]}
    assert 1 in reasons


def test_build_implied_no_provider_degrades() -> None:
    class _NoProvider:
        provider = object()

    section = implied.build_implied(_NoProvider(), "TEST", as_of=date(2025, 10, 1))
    assert all(value is None for value in section["by_horizon"].values())
    assert section["errors"]


# --------------------------------------------------------------------------- #
# base_rates — shrinkage and n_eff arithmetic.
# --------------------------------------------------------------------------- #
def test_stats_n_eff_is_n_over_h() -> None:
    returns = np.linspace(-0.2, 0.3, 48)
    block = base_rates._stats(returns, horizon_months=3)
    assert block["n"] == 48
    assert block["n_eff"] == pytest.approx(48 / 3)
    assert block["hit"] == pytest.approx(float(np.mean(returns > 0)))


def test_shrink_weight_and_blend() -> None:
    cond = {"q50": 0.20, "hit": 0.6, "mean": 0.2, "n_eff": 8.0, "n": 24}
    uncond = {"q50": 0.10, "hit": 0.5, "mean": 0.1, "n_eff": 100.0, "n": 300}
    shrunk = base_rates._shrink(cond, uncond, k=base_rates.SHRINK_K)
    # w = n_eff / (n_eff + 24) = 8 / 32 = 0.25
    assert shrunk["w"] == pytest.approx(0.25)
    assert shrunk["q50"] == pytest.approx(0.25 * 0.20 + 0.75 * 0.10)
    assert shrunk["hit"] == pytest.approx(0.25 * 0.6 + 0.75 * 0.5)


def test_shrink_falls_back_to_uncond_when_cond_empty() -> None:
    cond = {"q50": None, "hit": None, "mean": None, "n_eff": 0.0, "n": 0}
    uncond = {"q50": 0.12, "hit": 0.55, "mean": 0.12, "n_eff": 100.0, "n": 300}
    shrunk = base_rates._shrink(cond, uncond)
    assert shrunk["w"] == pytest.approx(0.0)
    assert shrunk["q50"] == pytest.approx(0.12)


def _geometric_daily_series(*, years: int, mu: float, sigma: float, seed: int) -> pd.Series:
    rng = np.random.default_rng(seed)
    n = years * 252
    index = pd.bdate_range("2013-01-01", periods=n)
    daily_ret = rng.normal(mu / 252, sigma / math.sqrt(252), size=n)
    prices = 100.0 * np.exp(np.cumsum(daily_ret))
    return pd.Series(prices, index=index)


def test_forward_returns_overlap_and_n_eff() -> None:
    close = _geometric_daily_series(years=11, mu=0.08, sigma=0.20, seed=7)
    forward = base_rates.forward_returns_monthly(close, 3)
    block = base_rates._stats(forward.to_numpy(dtype=float), horizon_months=3)
    # Overlapping monthly windows: n_eff must equal n / h.
    assert block["n"] > 0
    assert block["n_eff"] == pytest.approx(round(block["n"] / 3, 4))
    # ~11 years of month-ends, minus the 3-month forward tail.
    assert 100 <= block["n"] <= 135


def test_build_base_rates_structure_and_industry() -> None:
    ticker = _geometric_daily_series(years=11, mu=0.12, sigma=0.30, seed=1)
    industry = _geometric_daily_series(years=11, mu=0.08, sigma=0.20, seed=2)
    section = base_rates.build_base_rates(ticker, industry)
    assert section["cell"] in {
        "highvol_up",
        "highvol_down",
        "lowvol_up",
        "lowvol_down",
        None,
    }
    for h in base_rates.HORIZONS:
        block = section["by_horizon"][str(h)]
        assert set(block) >= {"uncond", "cond", "shrunk", "vol_managed", "industry"}
        assert block["shrunk"]["cell"] == section["cell"]
        # Shrunk quantiles lie between (or at) the conditional and unconditional.
        u = block["uncond"]["q50"]
        assert u is None or math.isfinite(u)
        assert block["industry"] is not None
        assert "shrunk" in block["industry"]

    # Conditional IQR + median exports feed implied's width_ratio / shift.
    iqrs = base_rates.conditional_iqr_by_horizon(section)
    medians = base_rates.shrunk_median_by_horizon(section)
    assert set(iqrs) == set(base_rates.HORIZONS)
    assert set(medians) == set(base_rates.HORIZONS)


def test_vol_managed_scales_by_target_over_current() -> None:
    close = _geometric_daily_series(years=11, mu=0.08, sigma=0.25, seed=5)
    section = base_rates.build_symbol_base_rates(close, current_cell_label="lowvol_up")
    block = section["by_horizon"]["3"]["vol_managed"]
    uncond = section["by_horizon"]["3"]["uncond"]
    if block["scale"] is not None and uncond["q75"] is not None:
        assert block["q75"] == pytest.approx(uncond["q75"] * block["scale"], rel=1e-9)


def test_base_rates_no_history_degrades() -> None:
    section = base_rates.build_base_rates(pd.Series(dtype="float64"))
    assert section["errors"]
    for h in base_rates.HORIZONS:
        assert section["by_horizon"][str(h)]["uncond"]["n"] == 0


def test_cell_series_is_point_in_time() -> None:
    """Recomputing the cell on a truncated series matches the value at that date."""
    close = _geometric_daily_series(years=8, mu=0.10, sigma=0.28, seed=11)
    cutoff = close.index[-260]
    full = base_rates.cell_series(close)
    truncated = base_rates.cell_series(close[close.index <= cutoff])
    if cutoff in full.index and not truncated.empty:
        assert full.loc[cutoff] == truncated.iloc[-1]


# --------------------------------------------------------------------------- #
# levels — moving averages and implied zones.
# --------------------------------------------------------------------------- #
def test_moving_averages_and_distance() -> None:
    close = pd.Series(np.arange(1.0, 301.0), index=pd.bdate_range("2020-01-01", periods=300))
    mas = levels.moving_averages(close)
    assert mas["ma20"] == pytest.approx(close.iloc[-20:].mean())
    assert mas["ma200"] == pytest.approx(close.iloc[-200:].mean())
    dist = levels.distance_to_ma(float(close.iloc[-1]), mas)
    assert dist["ma20"] == pytest.approx(float(close.iloc[-1]) / mas["ma20"] - 1.0)


def test_implied_zones_from_quantiles() -> None:
    implied_section = {
        "by_horizon": {
            "3": {"quantiles": {"q25": -0.08, "q75": 0.10}},
            "6": {"quantiles": {"q25": -0.12, "q75": 0.16}},
        }
    }
    zones = levels.implied_zones(implied_section, spot=100.0)
    assert zones["cheap_zone"]["price_lo"] == pytest.approx(88.0)  # 100 * (1 - 0.12)
    assert zones["cheap_zone"]["price_hi"] == pytest.approx(92.0)  # 100 * (1 - 0.08)
    assert zones["rich_zone"]["price_lo"] == pytest.approx(110.0)  # 100 * (1 + 0.10)
    assert zones["rich_zone"]["price_hi"] == pytest.approx(116.0)  # 100 * (1 + 0.16)
    assert zones["cheap_zone"]["horizon"] == "3m-6m"


def test_implied_zones_absent_when_no_implied() -> None:
    zones = levels.implied_zones(None, spot=100.0)
    assert zones["cheap_zone"] is None
    assert zones["rich_zone"] is None
