"""Offline tests for Situate's stack + validation (S4).

The load-bearing checks:

* **No leakage / IC recovery** — a synthetic cross-sectional panel with a known
  forward-return signal must yield a strongly positive walk-forward OOS IC and
  pass the publish gates; a pure-noise panel must be REJECTED (published=False).
* **Deflated Sharpe** — the PSR / deflation-benchmark formulae match hand-computed
  values (Bailey & López de Prado 2014).
* **Purge / embargo index math** — the training mask excludes exactly the months
  whose label windows overlap (or are within the embargo of) the test month.
* **peers** — the curated universe and its sector→ETF mapping are well formed.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from app.situate import peers, stack
from app.situate.validate import run_report, split_half_calibration, synthetic_panel


# --------------------------------------------------------------------------- #
# peers
# --------------------------------------------------------------------------- #
def test_peers_universe_and_etf_mapping() -> None:
    assert "AAPL" in peers.SP100
    assert peers.sector_of("AAPL") == "technology"
    assert peers.industry_etf_of("AAPL") == "XLK"
    assert peers.industry_etf_of("XOM") == "XLE"
    assert peers.industry_etf_of("NOTATICKER") is None
    tech = peers.peers_for("MSFT")
    assert "AAPL" in tech and "MSFT" in tech
    # universe_for guarantees the focus name is present even off-list.
    uni = peers.universe_for("AAPL")
    assert uni[0] == "AAPL"
    mapping = peers.etf_map(["AAPL", "XOM"])
    assert mapping == {"AAPL": "XLK", "XOM": "XLE"}
    assert set(peers.all_sector_etfs()) >= {"XLK", "XLE", "XLF", "XLV"}


# --------------------------------------------------------------------------- #
# purge / embargo index math
# --------------------------------------------------------------------------- #
def test_eligible_train_mask_purge_and_embargo() -> None:
    train_months = np.arange(0, 21, dtype=np.int64)
    mask = stack.eligible_train_mask(train_months, test_month=20, horizon=3, embargo=1)
    # eligible iff m + 3 <= 20 - 1 = 19  ->  m <= 16
    assert mask.tolist() == [m <= 16 for m in range(21)]
    # zero embargo, horizon 1: m + 1 <= 20 -> m <= 19
    mask0 = stack.eligible_train_mask(train_months, test_month=20, horizon=1, embargo=0)
    assert mask0.tolist() == [m <= 19 for m in range(21)]
    # a label that closes exactly on the test month is purged (must close strictly
    # before, by the embargo): m + h == test - embargo is allowed, one more is not.
    single = stack.eligible_train_mask(np.array([16, 17]), test_month=20, horizon=3, embargo=1)
    assert single.tolist() == [True, False]


# --------------------------------------------------------------------------- #
# deflated Sharpe — hand-computed
# --------------------------------------------------------------------------- #
def test_probabilistic_sharpe_hand_computed() -> None:
    # sr=0.2, sr*=0, T=100, normal moments:
    #   denom = 1 - 0 + (3-1)/4 * 0.04 = 1.02
    #   z = 0.2*sqrt(99)/sqrt(1.02) = 1.97037 ; Phi(1.97037) ~= 0.9756
    psr = stack.probabilistic_sharpe_ratio(0.2, 0.0, 100, skew=0.0, kurtosis=3.0)
    assert psr == pytest.approx(0.9756, abs=1e-3)


def test_expected_max_sharpe_hand_computed() -> None:
    # var=1, N=10:
    #   (1-gamma)*Phi^-1(0.9) + gamma*Phi^-1(1 - 1/(10e))
    #   = 0.4227843*1.2815516 + 0.5772157*1.789249 ~= 1.5746
    sr0 = stack.expected_max_sharpe(var_trials=1.0, n_trials=10)
    assert sr0 == pytest.approx(1.5746, abs=1e-2)
    # A single trial cannot be deflated.
    assert stack.expected_max_sharpe(var_trials=1.0, n_trials=1) == 0.0
    # More trials => a higher bar to clear.
    assert stack.expected_max_sharpe(1.0, 100) > stack.expected_max_sharpe(1.0, 10)


def test_deflated_sharpe_bundle() -> None:
    rng = np.random.default_rng(0)
    good = rng.normal(0.02, 0.01, size=120)  # a genuinely high Sharpe series
    ds = stack.deflated_sharpe(good, n_trials=1, var_trials=0.0)
    assert ds["sharpe"] > 0 and ds["sr0"] == 0.0
    assert ds["deflated_excess"] == pytest.approx(ds["sharpe"])
    # With many trials and dispersion, the benchmark bites.
    ds2 = stack.deflated_sharpe(good, n_trials=200, var_trials=0.5)
    assert ds2["sr0"] > 0 and ds2["deflated_excess"] < ds["deflated_excess"]


def test_sharpe_ratio_edges() -> None:
    assert math.isnan(stack.sharpe_ratio(np.array([1.0])))
    assert math.isnan(stack.sharpe_ratio(np.array([2.0, 2.0, 2.0])))  # zero vol
    assert stack.sharpe_ratio(np.array([1.0, -1.0, 1.0, -1.0])) == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# spearman IC + cross-sectional z-score
# --------------------------------------------------------------------------- #
def test_spearman_ic_and_zscore() -> None:
    up = np.array([1, 2, 3, 4.0])
    assert stack.spearman_ic(up, up) == pytest.approx(1.0)
    assert stack.spearman_ic(up, up[::-1]) == pytest.approx(-1.0)
    assert math.isnan(stack.spearman_ic(np.array([1.0, 2.0]), np.array([1.0, 2.0])))

    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2020-01-31"] * 3 + ["2020-02-29"] * 3),
            "symbol": ["A", "B", "C", "A", "B", "C"],
            "f": [1.0, 2.0, 3.0, 10.0, 20.0, 30.0],
        }
    )
    z = stack.cross_sectional_zscore(frame, ["f"])
    # Each date standardised independently -> identical z pattern both months.
    first = z[z["date"] == "2020-01-31"]["f"].to_numpy()
    second = z[z["date"] == "2020-02-29"]["f"].to_numpy()
    assert np.allclose(first, second)
    assert first.mean() == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# no-leakage / IC recovery on a known signal, rejection on noise
# --------------------------------------------------------------------------- #
_CFG = stack.StackConfig(
    horizons=(1, 3),
    min_train_months=24,
    min_train_rows=120,
    min_cross_section=8,
    n_bootstrap=300,
)


def test_stack_recovers_known_signal_and_publishes() -> None:
    frame = synthetic_panel(n_symbols=30, n_months=160, horizons=(1, 3), signal=1.0, seed=3)
    out = stack.run_stack_core(frame, ticker="SYM00", cfg=_CFG)
    assert out["universe_size"] == 30
    # The genuine signal lives in mom_12_1: IC at h=1 must be clearly positive.
    ic1 = out["by_horizon"]["1"]["oos_ic"]
    assert ic1 is not None and ic1 > 0.03
    ci = out["by_horizon"]["1"]["oos_ic_ci"]
    assert ci[0] is not None and ci[0] > 0.0  # bootstrap 90% CI excludes zero
    assert out["published"] is True
    assert "mom_12_1" in out["features"]
    # A prediction and a distribution are produced for the focus ticker.
    q = out["by_horizon"]["1"]["quantiles"]
    assert q["q50"] is not None
    assert q["q05"] <= q["q50"] <= q["q95"]
    assert out["configs_tried"] >= 1


def test_deflated_sharpe_uses_a_frozen_trial_set_across_horizons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every horizon must deflate against the identical (N, V): the trial count and

    the trial variance come from one fully-populated trial set. Freezing V before
    the pass-2 loop while N grew inside it mixed a variance from one config set
    with a count from another.
    """
    frame = synthetic_panel(n_symbols=30, n_months=160, horizons=(1, 3), signal=1.0, seed=3)
    calls: list[dict[str, float]] = []
    original = stack.deflated_sharpe

    def _spy(returns: np.ndarray, *, n_trials: int, var_trials: float) -> dict[str, float]:
        calls.append({"n_trials": float(n_trials), "var_trials": var_trials})
        return original(returns, n_trials=n_trials, var_trials=var_trials)

    monkeypatch.setattr(stack, "deflated_sharpe", _spy)
    out = stack.run_stack_core(frame, ticker="SYM00", cfg=_CFG)

    assert len(calls) >= 2  # one gate call per horizon
    assert len({c["n_trials"] for c in calls}) == 1, "trial COUNT must be frozen across horizons"
    assert len({round(c["var_trials"], 12) for c in calls}) == 1, "trial VARIANCE must be frozen"
    # The frozen count is the fully-populated trial set the packet reports.
    assert calls[0]["n_trials"] == float(out["configs_tried"]) == float(out["gates"]["n_trials"])


def test_stack_rejects_pure_noise() -> None:
    frame = synthetic_panel(n_symbols=30, n_months=160, horizons=(1, 3), signal=0.0, seed=99)
    out = stack.run_stack_core(frame, ticker="SYM00", cfg=_CFG)
    assert out["published"] is False
    assert out["reason"]
    for block in out["by_horizon"].values():
        # Noise IC hovers around zero; it must not clear the +0.03 gate with a
        # CI excluding zero.
        assert not block["passed_gates"]


def test_stack_thin_cross_section_does_not_publish() -> None:
    frame = synthetic_panel(n_symbols=4, n_months=160, horizons=(1,), signal=1.0, seed=1)
    out = stack.run_stack_core(frame, ticker="SYM00", cfg=_CFG)
    assert out["published"] is False
    assert "thin" in out["reason"].lower()


def test_ablation_drops_a_useless_feature() -> None:
    # Only mom_12_1 carries signal; removing rev_1m/dummies should not lower IC,
    # and at least one useless feature should be voted out.
    frame = synthetic_panel(n_symbols=30, n_months=160, horizons=(1,), signal=1.0, seed=7)
    cfg = stack.StackConfig(horizons=(1,), min_train_months=24, min_train_rows=120,
                            min_cross_section=8, n_bootstrap=200)
    out = stack.run_stack_core(frame, ticker="SYM00", cfg=cfg)
    assert "mom_12_1" in out["features"]
    assert out["ablations"], "ablation report should be populated"
    # The ablation entry for removing mom (the real signal) must show IC falling.
    mom_key = "h1:-mom_12_1"
    assert out["ablations"][mom_key]["raises_ic"] is False


# --------------------------------------------------------------------------- #
# calibration + validation report wiring
# --------------------------------------------------------------------------- #
def test_split_half_calibration_shape() -> None:
    frame = synthetic_panel(n_symbols=30, n_months=160, horizons=(1,), signal=1.0, seed=5)
    z = stack.cross_sectional_zscore(frame, list(stack.PRICE_FEATURES))
    oos = stack.walk_forward_oos(z, list(stack.PRICE_FEATURES), horizon=1, cfg=_CFG,
                                 target_col="target_h1")
    cal = split_half_calibration(oos["oos"])
    assert cal["coverage"] is None or (0.0 <= cal["coverage"] <= 1.0)


def test_run_report_smoke() -> None:
    frame = synthetic_panel(n_symbols=25, n_months=140, horizons=(1, 3), signal=1.0, seed=2)
    report = run_report(frame, ticker="SYM00", cfg=_CFG)
    assert "by_horizon" in report and "calibration" in report
    assert report["published"] in (True, False)


# --------------------------------------------------------------------------- #
# build_feature_panel from a Panel-like stub (no network)
# --------------------------------------------------------------------------- #
class _StubPanel:
    """A minimal object exposing daily_close(symbol) for build_feature_panel."""

    as_of = "2024-12-31"

    def __init__(self, closes: dict[str, pd.Series]) -> None:
        self._closes = closes

    def daily_close(self, symbol: str) -> pd.Series:
        return self._closes.get(str(symbol).upper(), pd.Series(dtype="float64"))


def _trend_series(seed: int, drift: float) -> pd.Series:
    idx = pd.bdate_range("2016-01-01", "2024-12-31")
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift, 0.01, size=len(idx))
    return pd.Series(100.0 * np.exp(np.cumsum(steps)), index=idx)


def test_build_feature_panel_from_panel() -> None:
    closes = {
        "AAPL": _trend_series(1, 0.0006),
        "MSFT": _trend_series(2, 0.0005),
        "NVDA": _trend_series(3, 0.0009),
        "XLK": _trend_series(4, 0.0004),
    }
    panel = _StubPanel(closes)
    etf_of = {"AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK"}
    frame, absent = stack.build_feature_panel(
        panel, ["AAPL", "MSFT", "NVDA"], etf_of=etf_of, horizons=(1, 3)
    )
    assert not frame.empty
    assert {"mom_12_1", "rev_1m", "target_h1", "target_h3"} <= set(frame.columns)
    # quality/value are honestly absent without fundamentals.
    assert "quality" in absent and "value" in absent
    # Targets are excess over XLK: a symbol identical to XLK would be ~0; here they
    # differ, so at least some targets are finite and non-trivial.
    assert frame["target_h1"].dropna().abs().sum() > 0
