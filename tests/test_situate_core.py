"""Offline deterministic tests for Situate's core (contract, panel, exposure, state).

Two load-bearing guarantees are pinned here:

* a synthetic ``1.2 * SPY + noise`` ticker recovers ``beta ~ 1.2`` (SPEC §8); and
* the **lookahead test**: recomputing at ``t`` after masking every observation
  after ``t`` yields a bit-identical exposure section (SPEC §4.5, §8).

A ``PRISM_LIVE``-guarded smoke test exercises the real Massive/FRED path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.situate import contract
from app.situate.exposure import (
    ExposureError,
    build_exposure,
    build_exposure_section,
    ewma_weights,
    ridge_fit,
)
from app.situate.factors_data import MIN_TRADING_DAYS_PER_MONTH, compound_to_monthly
from app.situate.panel import load_panel, monthly_log_returns
from app.situate.state import build_grid, build_hmm_opinion, build_state

# --------------------------------------------------------------------------
# contract
# --------------------------------------------------------------------------


def test_empty_packet_has_every_key_and_validates() -> None:
    packet = contract.empty_packet("nvda", as_of="2026-09-05")
    assert packet["ticker"] == "NVDA"
    assert packet["engine"] == "Situate"
    assert list(packet.keys())[: len(contract.PACKET_KEYS)] == list(contract.PACKET_KEYS)
    for section in contract.NULLABLE_SECTIONS:
        assert section in packet
        assert f"{section}_error" in packet
        assert packet[section] is None
        assert packet[f"{section}_error"] is None
    assert contract.validate_packet(packet) == []


def test_set_section_and_error_paths() -> None:
    packet = contract.empty_packet("MU")
    contract.set_section(packet, "exposure", {"betas": {"SPY": 1.0}})
    assert packet["exposure"] == {"betas": {"SPY": 1.0}}
    assert packet["exposure_error"] is None

    contract.set_section(packet, "state", None, error="no data")
    assert packet["state"] is None
    assert packet["state_error"] == "no data"
    assert {"source": "state", "error": "no data"} in packet["meta"]["errors"]
    assert contract.validate_packet(packet) == []

    contract.record_unavailable(packet, "implied:18m", "chain too thin")
    assert packet["meta"]["unavailable"][0]["source"] == "implied:18m"
    assert packet["meta"]["source_status"]["implied:18m"] == "unavailable"


def test_validate_packet_flags_contradiction() -> None:
    packet = contract.empty_packet("AAPL")
    packet["exposure"] = {"x": 1}
    packet["exposure_error"] = "boom"
    problems = contract.validate_packet(packet)
    assert any("exposure" in p for p in problems)


def test_horizon_and_quantile_helpers() -> None:
    assert contract.HORIZONS == (1, 2, 3, 6, 12, 18)
    assert contract.horizon_label(3) == "3m"
    assert contract.quantile_key(25) == "q25"
    assert contract.QUANTILE_KEYS == ("q05", "q25", "q50", "q75", "q95")
    with pytest.raises(ValueError):
        contract.horizon_label(4)


# --------------------------------------------------------------------------
# exposure math
# --------------------------------------------------------------------------


def test_ewma_weights_decay_and_normalise() -> None:
    w = ewma_weights(48, half_life=24)
    assert w.shape == (48,)
    assert w[-1] > w[0]
    assert w.sum() == pytest.approx(1.0)
    # 24 months of decay halves the weight.
    assert w[-25] / w[-1] == pytest.approx(0.5, rel=1e-9)


def test_ridge_recovers_ols_at_zero_lambda() -> None:
    rng = np.random.default_rng(0)
    n = 200
    x = rng.normal(size=(n, 2))
    true = np.array([0.7, -0.3])
    y = x @ true + 0.5 + rng.normal(scale=0.01, size=n)
    fit = ridge_fit(x, y, names=("a", "b"), lam=1e-8)
    assert fit.betas[0] == pytest.approx(0.7, abs=0.02)
    assert fit.betas[1] == pytest.approx(-0.3, abs=0.02)
    assert fit.alpha == pytest.approx(0.5, abs=0.02)
    assert fit.r2 > 0.99


def _synthetic_monthly(
    beta_spy: float = 1.2, *, months: int = 156, seed: int = 7
) -> tuple[pd.Series, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    index = pd.date_range(end="2026-08-31", periods=months, freq="ME")
    spy = rng.normal(0.008, 0.042, months)
    other = rng.normal(0.0, 0.03, months)
    y = beta_spy * spy + 0.0 * other + rng.normal(0.0, 0.004, months)
    basket = pd.DataFrame({"SPY": spy, "OTHER": other}, index=index)
    return pd.Series(y, index=index, name="NVDA"), basket


def test_synthetic_recovers_beta_1_2() -> None:
    y, basket = _synthetic_monthly(1.2)
    section = build_exposure(y, basket, n_boot=64, seed=1)
    betas = section["betas"]
    assert betas["SPY"] == pytest.approx(1.2, abs=0.06)
    assert abs(betas["OTHER"]) < 0.12
    assert 0.9 < section["r2"] <= 1.0
    assert section["idiosyncratic_share"] == pytest.approx(1.0 - section["r2"], abs=1e-9)
    assert section["method"] == "ewma_ridge"
    # Bootstrap SE present and the strong loading is many SE from zero.
    assert section["se"]["SPY"] is not None
    assert betas["SPY"] / section["se"]["SPY"] > 5.0


def test_build_exposure_needs_enough_months() -> None:
    y, basket = _synthetic_monthly(1.2, months=20)
    with pytest.raises(ExposureError):
        build_exposure(y, basket, min_months=36)


def test_change_6m_12m_are_single_differences() -> None:
    y, basket = _synthetic_monthly(1.2)
    section = build_exposure(y, basket, n_boot=32, seed=2)
    path = section["beta_path"]["SPY"]
    assert len(path) >= 13
    expected_6m = path[-1]["beta"] - path[-7]["beta"]
    assert section["change_6m"]["SPY"] == pytest.approx(expected_6m, abs=1e-12)
    expected_12m = path[-1]["beta"] - path[-13]["beta"]
    assert section["change_12m"]["SPY"] == pytest.approx(expected_12m, abs=1e-12)


def test_factor_view_from_synthetic_ken_french() -> None:
    rng = np.random.default_rng(3)
    months = 120
    index = pd.date_range(end="2026-08-31", periods=months, freq="ME")
    mkt = rng.normal(0.006, 0.04, months)
    factors = pd.DataFrame(
        {
            "MKT": mkt,
            "SMB": rng.normal(0, 0.02, months),
            "HML": rng.normal(0, 0.02, months),
            "RMW": rng.normal(0, 0.02, months),
            "CMA": rng.normal(0, 0.02, months),
            "MOM": rng.normal(0, 0.03, months),
            "RF": np.full(months, 0.002),
        },
        index=index,
    )
    y = 0.002 + 1.3 * mkt + factors["RF"].to_numpy() + rng.normal(0, 0.005, months)
    section = build_exposure(
        pd.Series(y, index=index), factors[["MKT"]].rename(columns={"MKT": "SPY"}),
        factors_monthly=factors, n_boot=16, seed=4,
    )
    view = section["factor"]
    assert view["error"] is None
    assert view["loadings"]["MKT"] == pytest.approx(1.3, abs=0.08)
    assert view["r2"] is not None and view["r2"] > 0.9


# --------------------------------------------------------------------------
# factors_data
# --------------------------------------------------------------------------


def test_compound_to_monthly_matches_hand_computation() -> None:
    dates = pd.date_range("2020-01-01", periods=40, freq="B")
    daily = pd.DataFrame({"MKT": np.full(40, 0.001), "RF": np.full(40, 0.0)}, index=dates)
    monthly = compound_to_monthly(daily)
    # January 2020 had >= MIN_TRADING_DAYS_PER_MONTH business days.
    jan = monthly.iloc[0]["MKT"]
    n_jan = int((dates.month == 1).sum())
    assert n_jan >= MIN_TRADING_DAYS_PER_MONTH
    assert jan == pytest.approx((1.001) ** n_jan - 1.0, rel=1e-9)


def test_compound_to_monthly_drops_partial_months() -> None:
    # Only 5 business days in the trailing month -> dropped.
    dates = pd.date_range("2021-03-01", periods=27, freq="B")
    daily = pd.DataFrame({"MKT": np.full(len(dates), 0.0), "RF": np.zeros(len(dates))}, index=dates)
    monthly = compound_to_monthly(daily)
    # March is complete (>=15 bdays), April has only ~5 -> only March survives.
    assert monthly.shape[0] == 1
    assert monthly.index[0].month == 3


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------


def _trending_series(n: int = 900, *, drift: float, vol: float, seed: int) -> pd.Series:
    rng = np.random.default_rng(seed)
    index = pd.date_range(end="2026-08-31", periods=n, freq="B")
    steps = rng.normal(drift, vol, n)
    prices = 100.0 * np.exp(np.cumsum(steps))
    return pd.Series(prices, index=index, name="X")


def test_build_grid_labels_cell() -> None:
    up = _trending_series(drift=0.0008, vol=0.01, seed=11)
    grid = build_grid(up, as_of="2026-08-31")
    assert grid["error"] is None
    assert grid["trend_state"] == "up"
    assert grid["vol_state"] in {"high", "low"}
    assert grid["cell"] == f"{grid['vol_state']}_up"
    assert grid["realized_vol_21d"] > 0
    assert grid["ret_12m_1m"] is not None


def test_build_grid_short_series_degrades() -> None:
    short = _trending_series(n=30, drift=0.0, vol=0.01, seed=12)
    grid = build_grid(short, as_of="2026-08-31")
    assert grid["error"] is not None
    assert grid["cell"] is None


def test_hmm_opinion_probs_sum_to_one() -> None:
    spy = _trending_series(n=1500, drift=0.0004, vol=0.011, seed=13)
    opinion = build_hmm_opinion(spy, as_of="2026-08-31", years=10)
    assert opinion is not None
    assert opinion["error"] is None
    total = sum(v for v in opinion["probs"].values() if v is not None)
    assert total == pytest.approx(1.0, abs=1e-6)
    assert opinion["label"] in {"bull", "neutral", "bear"}


def test_build_state_context_percentiles() -> None:
    spy = _trending_series(n=900, drift=0.0005, vol=0.01, seed=14)
    tkr = _trending_series(n=900, drift=0.0009, vol=0.02, seed=15)
    idx = spy.index
    vix = pd.Series(np.linspace(12, 30, len(idx)), index=idx)
    hy = pd.Series(np.linspace(3.0, 5.0, len(idx)), index=idx)
    dgs10 = pd.Series(np.full(len(idx), 4.2), index=idx)
    dgs2 = pd.Series(np.full(len(idx), 3.9), index=idx)
    state = build_state(
        spy_daily=spy, ticker_daily=tkr, vix=vix, hy_oas=hy, dgs10=dgs10, dgs2=dgs2,
        as_of="2026-08-31", run_hmm=False,
    )
    ctx = state["context"]
    # Current VIX is the max of a monotone series -> percentile ~ 1.0.
    assert ctx["vix_pct"] == pytest.approx(1.0, abs=1e-6)
    assert ctx["curve_10y_2y"] == pytest.approx(0.3, abs=1e-9)
    assert state["hmm"] is None
    assert state["spy"]["cell"] is not None


# --------------------------------------------------------------------------
# panel + lookahead (a synthetic in-memory market-data client)
# --------------------------------------------------------------------------


@dataclass
class _FakeHistory:
    data: pd.DataFrame
    provider: str = "fake"
    note: str = "synthetic"


class _FakeClient:
    """A deterministic market-data client generating correlated daily closes.

    ``get_history`` respects ``start``/``end`` exactly, so it also models the
    real Massive contract that never returns a bar after the requested ``end``.
    """

    def __init__(self, symbols: list[str], *, seed: int = 99, days: int = 2600) -> None:
        rng = np.random.default_rng(seed)
        index = pd.date_range(end="2026-08-31", periods=days, freq="B")
        market = rng.normal(0.0004, 0.011, days)
        self._series: dict[str, pd.Series] = {}
        betas = {"SPY": 1.0, "IWM": 1.1, "UUP": -0.2, "FXY": 0.1, "USO": 0.4, "GLD": 0.2}
        for sym in symbols:
            beta = betas.get(sym, rng.uniform(0.8, 1.6))
            idio = rng.normal(0.0, 0.008, days)
            steps = beta * market + idio
            prices = 100.0 * np.exp(np.cumsum(steps))
            self._series[sym] = pd.Series(prices, index=index, name=sym)

    def get_history(
        self, ticker: str, *, start: Any, end: Any, interval: str = "1d"
    ) -> _FakeHistory:
        del interval  # part of the provider contract; unused by the fake
        sym = str(ticker).upper()
        if sym not in self._series:
            raise ValueError(f"unknown symbol {sym}")
        series = self._series[sym]
        lo = pd.Timestamp(start)
        hi = pd.Timestamp(end)
        windowed = series[(series.index >= lo) & (series.index <= hi)]
        frame = pd.DataFrame({"Close": windowed})
        return _FakeHistory(data=frame)

    def get_profile(self, ticker: str) -> dict[str, Any]:
        return {"longName": ticker, "sector": "Technology"}


_PANEL_SYMBOLS = ["NVDA", "SPY", "IWM", "UUP", "FXY", "USO", "GLD"]


def test_panel_is_point_in_time() -> None:
    client = _FakeClient(_PANEL_SYMBOLS)
    as_of = date(2025, 6, 30)
    panel = load_panel(client, _PANEL_SYMBOLS, as_of=as_of, years=8)
    assert not panel.daily.empty
    assert panel.daily.index.max() <= pd.Timestamp(as_of)
    assert panel.monthly_log.index.max() <= pd.Timestamp(as_of)
    # Monthly log returns line up with a direct recomputation from the closes.
    direct = monthly_log_returns(panel.daily)["SPY"].dropna()
    assert panel.monthly_log_return("SPY").equals(direct)


def test_lookahead_masking_is_identical() -> None:
    """Deleting every observation after t must not change the exposure section."""
    as_of = date(2025, 6, 30)
    full = _FakeClient(_PANEL_SYMBOLS, seed=99)

    def _section(client: _FakeClient) -> dict[str, Any]:
        panel = load_panel(client, _PANEL_SYMBOLS, as_of=as_of, years=8)
        return build_exposure_section(
            panel, ticker="NVDA", sector_etf="SPY", n_boot=64, seed=5
        )

    section_full = _section(full)

    # A client whose data is physically truncated after t (nothing after t exists).
    masked = _FakeClient(_PANEL_SYMBOLS, seed=99)
    cutoff = pd.Timestamp(as_of)
    masked._series = {  # noqa: SLF001 - deliberately masking the fixture
        sym: s[s.index <= cutoff] for sym, s in masked._series.items()
    }
    section_masked = _section(masked)

    assert section_full["betas"] == section_masked["betas"]
    assert section_full["se"] == section_masked["se"]
    assert section_full["r2"] == section_masked["r2"]
    assert section_full["lambda_"] == section_masked["lambda_"]
    assert section_full["beta_path"] == section_masked["beta_path"]


def test_exposure_section_shape_from_panel() -> None:
    client = _FakeClient(_PANEL_SYMBOLS)
    panel = load_panel(client, _PANEL_SYMBOLS, as_of=date(2026, 6, 30), years=9)
    section = build_exposure_section(
        panel, ticker="NVDA", sector_etf="SPY", n_boot=32, seed=6
    )
    assert "SPY" in section["betas"]
    assert "IWM_SPY" in section["betas"]
    assert set(section["se"]) == set(section["betas"])
    assert section["n_months"] >= 36
    assert section["lambda_"] in section["cv"]["lambdas"] or section["lambda_"] > 0


# --------------------------------------------------------------------------
# live smoke (guarded)
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.getenv("PRISM_LIVE"), reason="set PRISM_LIVE=1 with real Massive/FRED keys"
)
def test_live_smoke_spy_self_beta() -> None:
    from app.prism.data import build_prism_client
    from app.prism.macro import fred_client_from_env
    from app.situate.factors_data import load_ken_french_monthly
    from app.situate.panel import load_macro_monthly
    from app.situate.state import state_section

    client = build_prism_client()
    fred = fred_client_from_env()
    symbols = ["SPY", "NVDA", "IWM", "UUP", "FXY", "USO", "GLD", "QQQ"]
    panel = load_panel(client, symbols, years=12)
    macro = load_macro_monthly(
        fred, ["DGS10", "DGS2", "BAMLH0A0HYM2", "VIXCLS"], as_of=panel.as_of
    )
    factors, _ = load_ken_french_monthly(as_of=panel.as_of)

    spy_exp = build_exposure_section(
        panel, ticker="SPY", macro_monthly=macro, factors_monthly=factors,
        sector_etf="QQQ", n_boot=100,
    )
    # SPY regressed on a basket that includes SPY: its own loading must be ~1.
    assert spy_exp["betas"]["SPY"] == pytest.approx(1.0, abs=0.15)
    assert spy_exp["r2"] > 0.95

    nvda_exp = build_exposure_section(
        panel, ticker="NVDA", macro_monthly=macro, factors_monthly=factors,
        sector_etf="QQQ", n_boot=100,
    )
    mkt_beta = nvda_exp["factor"]["loadings"].get("MKT")
    assert mkt_beta is None or 0.8 < mkt_beta < 2.5

    state = state_section(panel, ticker="NVDA", fred=fred)
    assert state["spy"]["cell"] is not None
    assert state["hmm"] is None or "probs" in state["hmm"]
