"""Tests for the Prism quantitative engine (W2).

Every test uses synthetic data with a structure known in advance, so a pass
means the estimator actually recovered the thing it claims to recover — a sine's
period, a Markov chain's regimes, a regression's betas — rather than merely
running without raising. No test touches the network; the Ken French path is
exercised through a fake session that serves a real-shaped ZIP payload.

A single live smoke test at the bottom runs the whole stack against Massive and
is skipped unless ``PRISM_LIVE=1``.
"""

from __future__ import annotations

import io
import json
import math
import os
import zipfile
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.prism import eigen, entropy, factors, hmm, regimes, scenarios, spectral

# --------------------------------------------------------------------------
# Synthetic fixtures
# --------------------------------------------------------------------------

TRADING_DAYS = 252


def _bdates(n: int, start: str = "2013-01-02") -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=n)


def _closes_from_returns(
    returns: np.ndarray, *, start: float = 100.0, first: str = "2013-01-02"
) -> pd.Series:
    prices = start * np.cumprod(1.0 + returns)
    return pd.Series(prices, index=_bdates(returns.shape[0], first), name="close")


def _three_regime_returns(
    n: int = 4000, seed: int = 7
) -> tuple[np.ndarray, np.ndarray, dict[int, str]]:
    """Markov-switching returns with a designed bull / neutral / bear ordering.

    State 0 is the bull regime (best mean return, lowest volatility), 1 the
    sideways regime, 2 the bear regime (most negative return, highest
    volatility) — exactly the arrangement the reference paper labels. Regimes are
    persistent (~60-70 day expected sojourns) and the bear regime is given ~18%
    stationary occupancy so its *sample* mean is reliably negative rather than a
    small-sample accident.
    """
    rng = np.random.default_rng(seed)
    means = np.array([0.0015, 0.0000, -0.0035])
    sigmas = np.array([0.004, 0.010, 0.025])
    transition = np.array(
        [
            [0.984, 0.014, 0.002],
            [0.016, 0.972, 0.012],
            [0.004, 0.026, 0.970],
        ]
    )
    states = np.zeros(n, dtype=np.int64)
    state = 0
    for index in range(n):
        states[index] = state
        state = int(rng.choice(3, p=transition[state]))
    returns = rng.normal(means[states], sigmas[states])
    return returns, states, {0: "bull", 1: "neutral", 2: "bear"}


@pytest.fixture(scope="module")
def three_regime() -> tuple[pd.Series, np.ndarray, dict[int, str]]:
    returns, states, truth = _three_regime_returns()
    return _closes_from_returns(returns), states, truth


@pytest.fixture(scope="module")
def sine_series() -> pd.Series:
    """Log price = linear trend + a clean 50-day sine + a whisper of noise."""
    n = 1000
    period = 50.0
    t = np.arange(n, dtype=float)
    rng = np.random.default_rng(3)
    log_price = np.log(100.0) + 0.0004 * t + 0.08 * np.sin(2 * np.pi * t / period) + rng.normal(
        0.0, 0.002, n
    )
    return pd.Series(np.exp(log_price), index=_bdates(n), name="close")


# --------------------------------------------------------------------------
# hmm.py
# --------------------------------------------------------------------------


class TestGaussianHMM:
    def test_recovers_two_well_separated_clusters(self) -> None:
        rng = np.random.default_rng(11)
        low = rng.normal([-3.0, 0.0], 0.3, size=(600, 2))
        high = rng.normal([3.0, 2.0], 0.3, size=(600, 2))
        observations = np.concatenate([low, high, low, high])

        model = hmm.fit_gaussian_hmm(observations, n_states=2, seed=5)

        recovered = sorted(model.means[:, 0])
        assert recovered[0] == pytest.approx(-3.0, abs=0.25)
        assert recovered[1] == pytest.approx(3.0, abs=0.25)
        assert model.transition.shape == (2, 2)
        np.testing.assert_allclose(model.transition.sum(axis=1), 1.0, atol=1e-9)
        assert model.covariances.shape == (2, 2, 2)

    def test_em_log_likelihood_is_monotone(self, three_regime: Any) -> None:
        closes, _, _ = three_regime
        features = regimes.regime_features(closes)
        raw = features.to_numpy(dtype=float)
        standardised = (raw - raw.mean(axis=0)) / raw.std(axis=0)
        model = hmm.fit_gaussian_hmm(standardised, n_states=3, seed=5)
        trace = np.asarray(model.log_likelihood_trace)
        assert trace.size >= 2
        # Baum-Welch is monotone; allow only floating-point sized dips.
        assert float(np.min(np.diff(trace))) > -1e-6

    def test_posteriors_and_viterbi_are_consistent(self) -> None:
        rng = np.random.default_rng(2)
        observations = np.concatenate(
            [rng.normal(-2.0, 0.4, 300), rng.normal(2.0, 0.4, 300)]
        ).reshape(-1, 1)
        model = hmm.fit_gaussian_hmm(observations, n_states=2, seed=1)
        posteriors = hmm.posterior_states(model, observations)
        path = hmm.viterbi(model, observations)

        np.testing.assert_allclose(posteriors.sum(axis=1), 1.0, atol=1e-9)
        assert path.shape == (600,)
        agreement = float(np.mean(path == np.argmax(posteriors, axis=1)))
        assert agreement > 0.98
        assert math.isfinite(hmm.score(model, observations))

    def test_fit_is_deterministic_for_a_seed(self, three_regime: Any) -> None:
        closes, _, _ = three_regime
        raw = regimes.regime_features(closes).to_numpy(dtype=float)
        standardised = (raw - raw.mean(axis=0)) / raw.std(axis=0)
        first = hmm.fit_gaussian_hmm(standardised, n_states=3, seed=99)
        second = hmm.fit_gaussian_hmm(standardised, n_states=3, seed=99)
        np.testing.assert_allclose(first.means, second.means)
        np.testing.assert_allclose(first.transition, second.transition)
        assert first.log_likelihood == pytest.approx(second.log_likelihood)

    def test_survives_perfectly_collinear_features(self) -> None:
        """A duplicated column makes every empirical covariance singular."""
        rng = np.random.default_rng(4)
        base = np.concatenate([rng.normal(-1.0, 0.2, 200), rng.normal(1.0, 0.2, 200)])
        observations = np.column_stack([base, base])
        model = hmm.fit_gaussian_hmm(observations, n_states=2, seed=3)
        assert np.all(np.isfinite(model.means))
        assert np.all(np.isfinite(model.covariances))
        assert math.isfinite(model.log_likelihood)

    def test_expected_durations_and_stationary_distribution(self) -> None:
        transition = np.array([[0.9, 0.1], [0.2, 0.8]])
        np.testing.assert_allclose(hmm.expected_durations(transition), [10.0, 5.0])
        stationary = hmm.stationary_distribution(transition)
        np.testing.assert_allclose(stationary, [2.0 / 3.0, 1.0 / 3.0], atol=1e-9)
        np.testing.assert_allclose(stationary @ transition, stationary, atol=1e-9)

    def test_rejects_unusable_input(self) -> None:
        with pytest.raises(hmm.HMMError):
            hmm.fit_gaussian_hmm(np.array([[1.0], [np.nan]]), n_states=2)
        with pytest.raises(hmm.HMMError):
            hmm.fit_gaussian_hmm(np.arange(3.0).reshape(-1, 1), n_states=3)


# --------------------------------------------------------------------------
# regimes.py
# --------------------------------------------------------------------------


class TestRegimeFeatures:
    def test_features_match_the_papers_definitions(self) -> None:
        rng = np.random.default_rng(0)
        closes = pd.Series(
            100.0 + np.cumsum(rng.normal(0.0, 1.0, 60)), index=_bdates(60), name="close"
        )
        frame = regimes.regime_features(closes, window=10, feature_scale="paper")

        # daily return
        expected_return = closes.iloc[-1] / closes.iloc[-2] - 1.0
        assert frame["daily_return"].iloc[-1] == pytest.approx(expected_return)

        # 10-day MSE around the 10-day moving average, evaluated by hand
        window = closes.iloc[-10:]
        moving_averages = closes.rolling(10).mean().iloc[-10:]
        by_hand = float(np.mean((window.to_numpy() - moving_averages.to_numpy()) ** 2))
        assert frame["vol_10d_mse"].iloc[-1] == pytest.approx(by_hand)

    def test_relative_scale_is_the_paper_form_divided_by_squared_ma(self) -> None:
        rng = np.random.default_rng(1)
        closes = pd.Series(
            100.0 + np.cumsum(rng.normal(0.0, 1.0, 60)), index=_bdates(60), name="close"
        )
        paper = regimes.regime_features(closes, feature_scale="paper")["vol_10d_mse"]
        relative = regimes.regime_features(closes, feature_scale="relative")["vol_10d_mse"]
        moving_average = closes.rolling(10).mean().reindex(paper.index)
        np.testing.assert_allclose(
            relative.to_numpy(), (paper / moving_average**2).to_numpy(), rtol=1e-10
        )

    def test_rejects_short_history(self) -> None:
        with pytest.raises(ValueError):
            regimes.regime_features(pd.Series([1.0, 2.0, 3.0]))


class TestLabelStates:
    def test_highest_return_is_bull_and_lowest_is_bear(self) -> None:
        labels = regimes.label_states([0.001, -0.002, 0.0001], [1.0, 9.0, 3.0])
        assert labels == {0: "bull", 1: "bear", 2: "neutral"}

    def test_ties_on_return_break_on_volatility(self) -> None:
        labels = regimes.label_states([0.001, 0.001, 0.0], [5.0, 1.0, 3.0])
        assert labels[1] == "bull"  # same return, lower volatility
        assert labels[2] == "bear"


class TestFitDistribution:
    def test_picks_a_sane_family_for_normal_data(self) -> None:
        rng = np.random.default_rng(12)
        fit = regimes.fit_distribution(rng.normal(0.0, 1.0, 4000))
        assert fit["family"] in {"normal", "laplace"}
        assert fit["ks_statistic"] < 0.1
        assert "normal" in fit["families_considered"]
        assert fit["omitted_families"] == ["gamma", "beta"]

    def test_positive_data_unlocks_the_positive_support_families(self) -> None:
        rng = np.random.default_rng(13)
        fit = regimes.fit_distribution(rng.lognormal(0.0, 0.5, 3000))
        assert set(fit["families_considered"]) >= {"normal", "lognormal", "exponential", "pareto"}
        assert fit["family"] == "lognormal"

    def test_short_samples_return_null_family(self) -> None:
        fit = regimes.fit_distribution([1.0, 2.0, 3.0])
        assert fit["family"] is None
        assert fit["n"] == 3


class TestBuildRegimes:
    def test_recovers_the_designed_three_regime_structure(self, three_regime: Any) -> None:
        closes, truth_states, truth_labels = three_regime
        section = regimes.build_regimes(closes, ticker_close=closes, trained_on="SYNTH")

        assert section["error"] is None
        assert section["n_states"] == 3
        by_label = {state["label"]: state for state in section["states"]}
        assert set(by_label) == {"bull", "neutral", "bear"}

        # Ordering the paper demands: bull has the highest mean return, bear the
        # lowest, and the bear regime carries the highest volatility.
        returns = {label: state["mean_daily_return"] for label, state in by_label.items()}
        assert returns["bull"] == max(returns.values())
        assert returns["bear"] == min(returns.values())
        vols = {label: state["volatility_annualized"] for label, state in by_label.items()}
        assert vols["bear"] == max(vols.values())

        # And the decoded sequence should track the chain that generated it. The
        # ceiling here is well below 1.0 by construction: the paper's volatility
        # feature is a 10-day trailing MSE, so it cannot see a regime change
        # until the window has refilled, and bull vs sideways days differ by only
        # 0.15% of daily mean. Chance is 0.33.
        decoded = np.asarray(_decoded_labels(closes))
        aligned_truth = np.asarray(
            [truth_labels[state] for state in truth_states[-decoded.shape[0] :]]
        )
        accuracy = float(np.mean(decoded == aligned_truth))
        assert accuracy > 0.6

    def test_section_shape_matches_the_packet_contract(self, three_regime: Any) -> None:
        closes, _, _ = three_regime
        section = regimes.build_regimes(closes, ticker_close=closes)

        for key in (
            "trained_on",
            "n_states",
            "features",
            "train_window_days",
            "states",
            "transition_matrix",
            "current",
            "ticker_by_regime",
            "history",
            "fitted_distributions",
        ):
            assert key in section

        matrix = np.asarray(section["transition_matrix"], dtype=float)
        np.testing.assert_allclose(matrix.sum(axis=1), 1.0, atol=1e-9)
        assert np.all(matrix >= 0.0)

        current = section["current"]
        assert current["label"] in {"bull", "neutral", "bear"}
        assert sum(current["posterior"]) == pytest.approx(1.0, abs=1e-9)
        assert 0.0 <= current["switch_confidence"] <= 1.0
        assert current["days_in_regime"] >= 1
        assert sum(current["next_state_probabilities"].values()) == pytest.approx(1.0, abs=1e-9)

        assert set(section["ticker_by_regime"]) == {"bull", "neutral", "bear"}
        assert section["history"], "monthly history should not be empty"
        assert all({"date", "state", "label"} <= set(entry) for entry in section["history"])
        # Monthly sampling: at most one entry per month.
        months = [entry["date"][:7] for entry in section["history"]]
        assert len(months) == len(set(months))

        # Expected duration is 1 / (1 - A_ii).
        for state in section["states"]:
            diagonal = matrix[state["id"], state["id"]]
            assert state["avg_duration_days"] == pytest.approx(1.0 / (1.0 - diagonal), rel=1e-9)

    def test_monthly_history_uses_real_trading_dates(self, three_regime: Any) -> None:
        closes, _, _ = three_regime
        section = regimes.build_regimes(closes)
        available = {str(pd.Timestamp(stamp).date()) for stamp in closes.index}
        for entry in section["history"]:
            assert entry["date"] in available

    def test_train_window_is_capped_at_the_paper_value(self) -> None:
        returns, _, _ = _three_regime_returns(n=3600, seed=21)
        closes = _closes_from_returns(returns)
        section = regimes.build_regimes(closes, train_window_days=regimes.DEFAULT_TRAIN_WINDOW_DAYS)
        assert section["train_window_days"] == 2707
        assert section["n_observations"] == 2707

    def test_short_history_reports_an_error_not_a_guess(self) -> None:
        closes = _closes_from_returns(np.full(40, 0.001))
        section = regimes.build_regimes(closes)
        assert section["error"]
        assert section["states"] == []
        assert section["current"] is None

    def test_ticker_stats_by_regime_flags_thin_buckets(self, three_regime: Any) -> None:
        closes, _, _ = three_regime
        fit = regimes.fit_regime_model(closes)
        labels = regimes.regime_state_series(fit)
        stats = regimes.ticker_stats_by_regime(closes, labels, min_observations=10**6)
        for entry in stats.values():
            assert entry["mean_daily"] is None
            assert "reason" in entry


def _decoded_labels(closes: pd.Series) -> list[str]:
    """Re-decode the fitted model so the test can compare against the truth."""
    fit = regimes.fit_regime_model(closes)
    return [str(value) for value in regimes.regime_state_series(fit).to_numpy()]


# --------------------------------------------------------------------------
# entropy.py
# --------------------------------------------------------------------------


class TestEntropy:
    def test_uniform_noise_reads_as_noise_on_the_fixed_grid(self) -> None:
        rng = np.random.default_rng(5)
        value = entropy.shannon_entropy(rng.uniform(-1.0, 1.0, 20_000), bins=10)
        # A uniform sample fills the middle of a [-3 sigma, +3 sigma] grid; it
        # cannot reach exactly 1 (a uniform's own range is +/-1.73 sigma) but it
        # is unambiguously above the noise threshold.
        assert value > entropy.NOISE_THRESHOLD
        assert entropy.classify_entropy(value) == "noise"

    def test_tightly_clustered_returns_read_as_structure(self) -> None:
        """A quiet tape inside a wide long-run range must score low.

        The grid is scaled by the *full-sample* sigma, which a handful of large
        moves sets. A window that then trades in a narrow band lands in one or
        two cells of that grid, which is exactly what "structure" is supposed to
        mean — and what the old full-sample quantile grid could never report.
        """
        rng = np.random.default_rng(77)
        quiet = rng.normal(0.0, 0.0006, 900)
        shocks = np.array([0.12, -0.11, 0.09, -0.10, 0.13, -0.14])
        sample = np.concatenate([quiet, shocks])
        sigma = entropy.full_sample_sigma(sample)
        edges = entropy.fixed_width_bin_edges(sigma, bins=10)

        value = entropy.shannon_entropy(quiet, bins=10, edges=edges)
        assert value < entropy.STRUCTURE_THRESHOLD
        assert entropy.classify_entropy(value) == "structure"
        # The superseded quantile grid calls the very same window noise.
        quantile_edges = entropy.quantile_bin_edges(sample, bins=10)
        assert entropy.shannon_entropy(quiet, bins=10, edges=quantile_edges) > value

    def test_both_thresholds_are_reachable_on_one_grid(self) -> None:
        """structure / mixed / noise must all be attainable readings."""
        rng = np.random.default_rng(101)
        sigma = 0.02
        edges = entropy.fixed_width_bin_edges(sigma, bins=10)
        readings = [
            entropy.shannon_entropy(rng.normal(0.0, sigma * 0.12, 4000), edges=edges),
            entropy.shannon_entropy(rng.normal(0.0, sigma * 0.55, 4000), edges=edges),
            entropy.shannon_entropy(rng.uniform(-3 * sigma, 3 * sigma, 4000), edges=edges),
        ]
        assert [entropy.classify_entropy(value) for value in readings] == [
            "structure",
            "mixed",
            "noise",
        ]
        assert readings[0] < readings[1] < readings[2]

    def test_fixed_width_edges_span_three_sigma_and_reject_a_dead_scale(self) -> None:
        edges = entropy.fixed_width_bin_edges(0.02, bins=10)
        assert edges.size == 11
        assert edges[0] == pytest.approx(-0.06)
        assert edges[-1] == pytest.approx(0.06)
        assert np.allclose(np.diff(edges), 0.012)
        with pytest.raises(ValueError):
            entropy.fixed_width_bin_edges(0.0, bins=10)

    def test_outliers_are_clipped_into_the_edge_bins(self) -> None:
        edges = entropy.fixed_width_bin_edges(0.01, bins=10)
        inside = entropy.shannon_entropy(np.full(100, 0.005), edges=edges)
        # A 50-sigma move is still just "the top bin", not a new cell.
        assert entropy.shannon_entropy(np.full(100, 0.5), edges=edges) == inside == 0.0

    def test_constant_series_is_zero_entropy(self) -> None:
        assert entropy.shannon_entropy(np.full(500, 0.004), bins=10) == 0.0

    def test_a_two_valued_series_sits_at_log2_2_over_log2_10(self) -> None:
        values = np.concatenate([np.full(500, -0.01), np.full(500, 0.01)])
        assert entropy.shannon_entropy(values, bins=10) == pytest.approx(
            math.log2(2) / math.log2(10), abs=1e-9
        )

    def test_a_concentrated_window_scores_below_the_full_sample(self) -> None:
        """The legacy quantile grid still behaves, since `H_quantile` reports it."""
        rng = np.random.default_rng(6)
        sample = rng.normal(0.0, 0.01, 4000)
        edges = entropy.quantile_bin_edges(sample, bins=10)
        full = entropy.shannon_entropy(sample, bins=10, edges=edges)
        # A window that lives in the right tail only touches a couple of bins.
        concentrated = np.abs(rng.normal(0.0, 0.001, 200)) + float(np.quantile(sample, 0.9))
        window = entropy.shannon_entropy(concentrated, bins=10, edges=edges)
        assert full > 0.95
        assert window < 0.4
        assert entropy.classify_entropy(window) == "structure"
        assert entropy.classify_entropy(full) == "noise"

    def test_classification_thresholds(self) -> None:
        assert entropy.classify_entropy(0.2) == "structure"
        assert entropy.classify_entropy(0.5) == "mixed"
        assert entropy.classify_entropy(0.9) == "noise"
        assert entropy.classify_entropy(None) == "unknown"

    def test_build_entropy_section(self, three_regime: Any) -> None:
        closes, _, _ = three_regime
        section = entropy.build_entropy(closes)

        assert section["error"] is None
        assert section["bins"] == 10
        assert section["bin_grid"] == "fixed_width_3sigma"
        assert section["sigma_full_sample"] > 0
        # The grid is exactly [-3 sigma, +3 sigma] in `bins` equal cells.
        assert section["bin_edges"][0] == pytest.approx(-3.0 * section["sigma_full_sample"])
        assert section["bin_edges"][-1] == pytest.approx(3.0 * section["sigma_full_sample"])
        assert len(section["bin_edges"]) == 11
        assert set(section["windows"]) == {"1m", "2m", "3m", "6m", "12m"}
        for window in section["windows"].values():
            if window["H"] is not None:
                assert 0.0 <= window["H"] <= 1.0
                assert window["classification"] in {"structure", "mixed", "noise"}
                assert window["bin_grid"] == "fixed_width_3sigma"
                # The superseded quantile reading is kept, not the label.
                assert 0.0 <= window["H_quantile"] <= 1.0
        assert section["series"]
        assert all(0.0 <= point["H"] <= 1.0 for point in section["series"])

        backtest = section["backtest"]
        assert backtest["n_low"] > 0 and backtest["n_high"] > 0
        assert 0.0 <= backtest["low_entropy_win_rate"] <= 1.0
        assert 0.0 <= backtest["high_entropy_win_rate"] <= 1.0
        assert backtest["edge"] == pytest.approx(
            backtest["low_entropy_win_rate"] - backtest["high_entropy_win_rate"]
        )
        assert backtest["split"] in {"fixed_threshold", "tercile"}

    def test_windows_carry_a_percentile_relative_to_the_tickers_own_history(
        self, three_regime: Any
    ) -> None:
        closes, _, _ = three_regime
        section = entropy.build_entropy(closes)
        for window in section["windows"].values():
            if window["H"] is None:
                continue
            assert 0.0 <= window["percentile"] <= 1.0
            assert window["relative_classification"] in {
                "structure_vs_own_history",
                "typical",
                "noise_vs_own_history",
            }
            assert window["history_min"] <= window["history_median"] <= window["history_max"]

    def test_rolling_series_matches_the_direct_computation(self) -> None:
        rng = np.random.default_rng(41)
        returns = pd.Series(rng.normal(0.0, 0.01, 400), index=_bdates(400))
        edges = entropy.quantile_bin_edges(returns, bins=10)
        rolling = entropy.entropy_series(returns, window=63, bins=10, edges=edges)
        direct = entropy.shannon_entropy(returns.iloc[-63:], bins=10, edges=edges)
        default_grid = entropy.entropy_series(returns, window=63, bins=10)
        # No explicit edges means the fixed-width grid, not the quantile one.
        assert default_grid.iloc[-1] == pytest.approx(
            entropy.shannon_entropy(
                returns.iloc[-63:],
                bins=10,
                edges=entropy.fixed_width_bin_edges(entropy.full_sample_sigma(returns), bins=10),
            )
        )
        assert rolling.iloc[-1] == pytest.approx(direct)
        assert rolling.shape[0] == 400 - 63 + 1

    def test_short_history_reports_a_reason(self) -> None:
        section = entropy.build_entropy(pd.Series([100.0, 101.0, 102.0]))
        assert section["error"]
        assert all(window["H"] is None for window in section["windows"].values())


# --------------------------------------------------------------------------
# spectral.py
# --------------------------------------------------------------------------


class TestSpectral:
    def test_recovers_a_known_period(self, sine_series: pd.Series) -> None:
        residual, trend = spectral.detrend_log_price(sine_series)
        modes = spectral.spectral_modes(residual, top_k=3)

        assert modes, "expected at least one surviving mode"
        assert modes[0]["period_days"] == pytest.approx(50.0, rel=0.05)
        assert modes[0]["power_share"] > 0.8
        # Trend recovered too: 0.0004 log points/day.
        assert trend["slope_per_day"] == pytest.approx(0.0004, rel=0.05)

    def test_reconstruction_explains_the_synthetic_cycle(self, sine_series: pd.Series) -> None:
        section = spectral.build_spectral(sine_series, top_k=3)
        assert section["error"] is None
        assert section["reconstruction_r2"] > 0.9
        assert section["dominant_period_days"] == pytest.approx(50.0, rel=0.05)

    def test_cycle_position_labels(self) -> None:
        assert spectral.cycle_position(0.0)[0] == "peak"
        assert spectral.cycle_position(math.pi)[0] == "trough"
        assert spectral.cycle_position(math.pi / 2)[0] == "falling"
        assert spectral.cycle_position(3 * math.pi / 2)[0] == "rising"
        _, fraction = spectral.cycle_position(math.pi)
        assert fraction == pytest.approx(0.5)

    def test_projection_shape_and_bounds(self, sine_series: pd.Series) -> None:
        section = spectral.build_spectral(sine_series)
        assert set(section["projection"]) == set(spectral.SPECTRAL_HORIZONS)
        for label, entry in section["projection"].items():
            assert 0.0 <= entry["confidence"] <= 1.0
            assert math.isfinite(entry["expected_return"])
            assert entry["horizon_days"] == spectral.SPECTRAL_HORIZONS[label]
        # Confidence must decay with horizon for a fixed fit.
        confidences = [section["projection"][label]["confidence"] for label in ("1m", "6m", "18m")]
        assert confidences[0] > confidences[1] > confidences[2]

    def test_consistency_is_reported_for_a_clean_cycle(self, sine_series: pd.Series) -> None:
        section = spectral.build_spectral(sine_series, top_k=3)
        consistency = section["consistency"]
        assert consistency["likelihood_label"] in {"consistent", "drifting", "broken"}
        assert consistency["recent_fit_error"] is not None
        assert consistency["z"] is not None

    def test_a_random_walk_concentrates_far_less_power_than_a_real_cycle(
        self, sine_series: pd.Series
    ) -> None:
        """A random walk has no cycle, and the estimator must say so.

        Detrended log prices of a random walk still produce Fourier modes — the
        1/f^2 shape guarantees the longest admissible period wins — so the honest
        test is that the winning mode carries far less of the power than a real
        periodic component does, and that the reconstruction explains far less.
        """
        rng = np.random.default_rng(8)
        noise = spectral.build_spectral(
            _closes_from_returns(rng.normal(0.0, 0.01, 1000)), top_k=3
        )
        cycle = spectral.build_spectral(sine_series, top_k=3)
        assert noise["error"] is None
        assert noise["modes"][0]["power_share"] < 0.5
        assert cycle["modes"][0]["power_share"] > 2 * noise["modes"][0]["power_share"]
        assert noise["reconstruction_r2"] < 0.6 < cycle["reconstruction_r2"]

    def test_short_history_reports_an_error(self) -> None:
        section = spectral.build_spectral(pd.Series([100.0, 101.0]))
        assert section["error"]


# --------------------------------------------------------------------------
# eigen.py
# --------------------------------------------------------------------------


class TestEigen:
    def test_rank_one_matrix_is_explained_by_one_component(self) -> None:
        rng = np.random.default_rng(9)
        u = rng.normal(size=400)
        v = np.array([1.0, -2.0, 0.5, 3.0])
        matrix = np.outer(u, v)
        result = eigen.pca_svd(matrix)
        assert result["error"] is None
        assert result["explained_variance_ratio"][0] == pytest.approx(1.0, abs=1e-9)
        assert sum(result["explained_variance_ratio"][1:]) == pytest.approx(0.0, abs=1e-9)

    def test_two_independent_blocks_split_the_variance(self) -> None:
        rng = np.random.default_rng(10)
        a = rng.normal(size=500)
        b = rng.normal(size=500)
        frame = pd.DataFrame({"a1": a, "a2": a, "b1": b, "b2": b})
        result = eigen.pca_svd(frame)
        top_two = sum(result["explained_variance_ratio"][:2])
        assert top_two == pytest.approx(1.0, abs=1e-9)
        assert result["explained_variance_ratio"][0] == pytest.approx(0.5, abs=0.05)

    def test_gauge_fix_standardises_and_drops_constants(self) -> None:
        frame = pd.DataFrame({"a": [1.0, 2.0, 3.0, 4.0], "flat": [7.0] * 4})
        fixed, dropped = eigen.gauge_fix(frame)
        assert dropped == ["flat"]
        assert float(fixed["a"].mean()) == pytest.approx(0.0, abs=1e-12)
        assert float(fixed["a"].std(ddof=0)) == pytest.approx(1.0)

    def test_rank_signals_orders_by_absolute_correlation(self) -> None:
        rng = np.random.default_rng(14)
        n = 400
        target = pd.Series(rng.normal(0.0, 0.01, n), index=_bdates(n))
        signals = pd.DataFrame(
            {
                "strong": target * 3.0 + rng.normal(0.0, 0.001, n),
                "inverse": -target * 2.0 + rng.normal(0.0, 0.002, n),
                "noise": rng.normal(0.0, 0.01, n),
            },
            index=target.index,
        )
        ranking = eigen.rank_signals(signals, target, windows={"1y": 252, "3m": 63})
        assert [row["signal"] for row in ranking][:2] == ["strong", "inverse"]
        assert ranking[0]["rank"] == 1
        assert ranking[0]["corr_1y"] > 0.9
        assert ranking[1]["corr_1y"] < -0.7
        assert abs(ranking[2]["corr_1y"]) < 0.3

    def test_symmetry_detects_a_designed_sign_flip(self) -> None:
        rng = np.random.default_rng(15)
        n = 400
        index = _bdates(n)
        labels = pd.Series(["bull"] * 200 + ["bear"] * 200, index=index)
        base = rng.normal(size=n)
        flipper = np.concatenate([base[:200], -base[200:]]) + rng.normal(0.0, 0.15, n)
        stable = base * 0.9 + rng.normal(0.0, 0.15, n)
        signals = pd.DataFrame({"base": base, "flipper": flipper, "stable": stable}, index=index)

        symmetry = eigen.regime_correlation_flips(signals, labels)
        broken = {tuple(sorted(entry["pair"])) for entry in symmetry["broken_pairs"]}
        invariant = {tuple(sorted(entry["pair"])) for entry in symmetry["gauge_invariant_pairs"]}
        assert ("base", "flipper") in broken
        assert ("base", "stable") in invariant
        assert set(symmetry["regimes_used"]) == {"bear", "bull"}

    def test_symmetry_needs_two_populated_regimes(self) -> None:
        index = _bdates(50)
        signals = pd.DataFrame({"a": np.arange(50.0), "b": np.arange(50.0)}, index=index)
        symmetry = eigen.regime_correlation_flips(signals, pd.Series(["bull"] * 50, index=index))
        assert symmetry["reason"]
        assert symmetry["broken_pairs"] == []

    def test_load_bearing_is_interventional_not_correlational(self) -> None:
        def weight_fn(names: Any) -> dict[str, float]:
            # "heavy" is the only signal that actually moves the weights.
            if "heavy" in names:
                return {"regime": 0.7, "macro": 0.3}
            return {"regime": 0.4, "macro": 0.6}

        rows = eigen.load_bearing_test(["heavy", "decorative"], weight_fn, threshold=0.1)
        by_name = {row["signal"]: row for row in rows}
        assert by_name["heavy"]["load_bearing"] is True
        assert by_name["heavy"]["weight_delta_if_removed"] == pytest.approx(0.6)
        assert by_name["decorative"]["load_bearing"] is False
        assert by_name["decorative"]["weight_delta_if_removed"] == pytest.approx(0.0)

    def test_load_bearing_reports_a_failing_callback(self) -> None:
        def boom(_names: Any) -> dict[str, float]:
            raise RuntimeError("no weights today")

        rows = eigen.load_bearing_test(["a"], boom)
        assert rows[0]["load_bearing"] is None
        assert "no weights today" in rows[0]["error"]

    def test_build_eigen_section(self) -> None:
        rng = np.random.default_rng(16)
        n = 500
        index = _bdates(n)
        target = pd.Series(rng.normal(0.0, 0.01, n), index=index)
        signals = pd.DataFrame(
            {
                "spy": target * 1.5 + rng.normal(0.0, 0.003, n),
                "vix": -target * 0.8 + rng.normal(0.0, 0.005, n),
                "junk": rng.normal(0.0, 0.01, n),
            },
            index=index,
        )
        labels = pd.Series(["bull"] * 250 + ["bear"] * 250, index=index)
        section = eigen.build_eigen(
            signals,
            target,
            regime_labels=labels,
            weight_fn=lambda names: {"regime": 1.0 / max(len(list(names)), 1)},
            windows={"1y": 252, "6m": 126, "3m": 63},
        )
        assert section["error"] is None
        assert section["feature_names"] == ["spy", "vix", "junk"]
        assert len(section["pca"]["explained_variance_ratio"]) == 3
        assert sum(section["pca"]["explained_variance_ratio"]) == pytest.approx(1.0, abs=1e-9)
        assert section["signal_ranking"][0]["signal"] == "spy"
        assert section["load_bearing"]
        assert section["reference_frame"].startswith("zscored")

    def test_build_eigen_without_signals(self) -> None:
        section = eigen.build_eigen(pd.DataFrame(), pd.Series(dtype=float))
        assert section["error"] == "no signals supplied"


# --------------------------------------------------------------------------
# factors.py
# --------------------------------------------------------------------------

_FF5_SAMPLE = """This file was created by using the 202606 CRSP database.
The Tbill return is the simple daily rate.

,Mkt-RF,SMB,HML,RMW,CMA,RF
20240102,   -0.67,    0.00,   -0.34,   -0.01,    0.16,    0.02
20240103,    0.79,   -0.26,    0.26,   -0.07,   -0.20,    0.02
20240104,    0.63,   -0.17,   -0.09,    0.18,   -0.34,    0.02
20240105,  -99.99,  -99.99,  -99.99,  -99.99,  -99.99,    0.02

Copyright 2026 Eugene F. Fama and Kenneth R. French
"""

_MOM_SAMPLE = """This file was created by using the 202606 CRSP database.
Missing data are indicated by -99.99 or -999.

,Mom
20240102,   0.35
20240103,  -0.61
20240104,   1.20
20240105,   0.10

Copyright 2026 Eugene F. Fama and Kenneth R. French
"""


def _zip_bytes(name: str, payload: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, payload)
    return buffer.getvalue()


class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url: str, timeout: float = 30.0) -> _FakeResponse:  # noqa: ARG002
        self.calls.append(url)
        if "5_Factors" in url:
            return _FakeResponse(
                _zip_bytes("F-F_Research_Data_5_Factors_2x3_daily.csv", _FF5_SAMPLE)
            )
        return _FakeResponse(_zip_bytes("F-F_Momentum_Factor_daily.csv", _MOM_SAMPLE))


class _BrokenSession:
    def get(self, url: str, timeout: float = 30.0) -> _FakeResponse:  # noqa: ARG002
        raise OSError("network is down")


class TestKenFrench:
    def test_parses_percent_into_decimals_and_masks_sentinels(self) -> None:
        frame = factors._parse_ken_french_csv(_FF5_SAMPLE)
        assert list(frame.columns) == ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"]
        assert frame.loc["2024-01-02", "Mkt-RF"] == pytest.approx(-0.0067)
        assert frame.loc["2024-01-03", "SMB"] == pytest.approx(-0.0026)
        assert math.isnan(frame.loc["2024-01-05", "Mkt-RF"])
        assert frame.index[0] == pd.Timestamp("2024-01-02")

    def test_download_joins_momentum_and_writes_a_cache(self, tmp_path: Any) -> None:
        session = _FakeSession()
        frame, provenance = factors.download_ken_french_factors(
            cache_dir=tmp_path, session=session
        )
        assert list(frame.columns) == ["MKT", "SMB", "HML", "RMW", "CMA", "MOM", "RF"]
        assert frame.loc["2024-01-04", "MOM"] == pytest.approx(0.012)
        assert provenance["from_cache"] is False
        assert provenance["provider"] == "ken_french_data_library"
        assert provenance["cache_path"] is not None

        # Second call is served from the cache without touching the session.
        session.calls.clear()
        cached, cached_provenance = factors.download_ken_french_factors(
            cache_dir=tmp_path, session=session
        )
        assert session.calls == []
        assert cached_provenance["from_cache"] is True
        pd.testing.assert_frame_equal(frame, cached)

    def test_download_failure_falls_back_to_a_stale_cache(self, tmp_path: Any) -> None:
        factors.download_ken_french_factors(cache_dir=tmp_path, session=_FakeSession())
        frame, provenance = factors.download_ken_french_factors(
            cache_dir=tmp_path, session=_BrokenSession(), use_cache=False
        )
        assert provenance["stale"] is True
        assert "network is down" in provenance["download_error"]
        assert not frame.empty

    def test_download_failure_without_a_cache_raises(self, tmp_path: Any) -> None:
        with pytest.raises(RuntimeError, match="download failed"):
            factors.download_ken_french_factors(
                cache_dir=tmp_path / "empty", session=_BrokenSession(), use_cache=False
            )


class TestOls:
    def test_recovers_known_coefficients_with_significant_t_stats(self) -> None:
        rng = np.random.default_rng(17)
        n = 2000
        mkt = rng.normal(0.0004, 0.01, n)
        smb = rng.normal(0.0, 0.006, n)
        y = 0.0002 + 1.35 * mkt - 0.40 * smb + rng.normal(0.0, 0.002, n)

        result = factors.ols_with_stats(
            y, np.column_stack([mkt, smb]), feature_names=["MKT", "SMB"]
        )
        assert result["betas"]["MKT"] == pytest.approx(1.35, abs=0.02)
        assert result["betas"]["SMB"] == pytest.approx(-0.40, abs=0.05)
        assert result["alpha"] == pytest.approx(0.0002, abs=1e-4)
        assert result["t_stats"]["MKT"] > 50
        assert result["t_stats"]["SMB"] < -5
        assert result["r2"] > 0.95
        assert result["n"] == n
        assert result["dof"] == n - 3

    def test_rejects_an_underdetermined_system(self) -> None:
        with pytest.raises(ValueError, match="not enough observations"):
            factors.ols_with_stats([1.0, 2.0], [[1.0, 2.0], [3.0, 4.0]], feature_names=["a", "b"])


class TestFactorSection:
    @staticmethod
    def _synthetic(
        seed: int = 18, n: int = 1600
    ) -> tuple[pd.Series, pd.DataFrame, dict[str, float]]:
        rng = np.random.default_rng(seed)
        index = _bdates(n)
        frame = pd.DataFrame(
            {
                "MKT": rng.normal(0.0004, 0.010, n),
                "SMB": rng.normal(0.0000, 0.005, n),
                "HML": rng.normal(0.0000, 0.005, n),
                "RMW": rng.normal(0.0000, 0.004, n),
                "CMA": rng.normal(0.0000, 0.004, n),
                "MOM": rng.normal(0.0001, 0.006, n),
                "RF": np.full(n, 0.00008),
            },
            index=index,
        )
        betas = {"MKT": 1.6, "SMB": 0.5, "HML": -0.8, "RMW": 0.2, "CMA": -0.1, "MOM": 0.3}
        excess = sum(frame[name] * beta for name, beta in betas.items())
        returns = excess + frame["RF"] + rng.normal(0.0, 0.004, n)
        closes = pd.Series(100.0 * np.cumprod(1.0 + returns.to_numpy()), index=index, name="close")
        return closes, frame, betas

    def test_recovers_known_betas_per_window(self) -> None:
        closes, frame, betas = self._synthetic()
        section = factors.build_factors(closes, factors=frame)

        assert section["error"] is None
        assert section["model"] == "fama_french_5_mom"
        assert set(section["windows"]) == {"1y", "3y", "5y", "10y"}

        window = section["windows"]["3y"]
        assert window["error"] is None
        for name, expected in betas.items():
            assert window["betas"][name] == pytest.approx(expected, abs=0.08)
        assert window["r2"] > 0.85
        assert window["t_stats"]["MKT"] > 20
        assert window["residual_vol_annual"] == pytest.approx(0.004 * math.sqrt(252), rel=0.15)
        assert window["n"] == 756
        assert set(window["factor_means"]) == set(betas)

    def test_long_window_reports_an_error_when_history_is_short(self) -> None:
        closes, frame, _ = self._synthetic(n=400)
        section = factors.build_factors(closes, factors=frame)
        assert section["windows"]["1y"]["error"] is None
        assert section["windows"]["10y"]["error"] is None  # capped at the available rows
        assert section["windows"]["10y"]["n"] < 400

    def test_residual_block_is_populated(self) -> None:
        closes, frame, _ = self._synthetic()
        section = factors.build_factors(closes, factors=frame)
        residuals = section["residuals"]
        assert residuals["last_20d_cum"] is not None
        assert residuals["last_60d_cum"] is not None
        assert residuals["z_score"] is not None
        assert abs(residuals["z_score"]) < 6.0
        assert residuals["window_used"] in {"1y", "3y", "5y", "10y"}

    def test_etf_proxy_definitions_are_explicit(self) -> None:
        rng = np.random.default_rng(19)
        n = 900
        index = _bdates(n)
        closes = {
            symbol: pd.Series(
                100.0 * np.cumprod(1.0 + rng.normal(0.0003, 0.01, n)), index=index
            )
            for symbol in ("SPY", "IWM", "IWD", "IWF", "QUAL", "XLU", "XLI", "MTUM")
        }
        frame, provenance = etf = factors.etf_proxy_factors(closes, risk_free_annual=0.04)
        assert provenance["model"] == "etf_proxy"
        assert provenance["definitions"]["SMB"] == "IWM - SPY"
        assert provenance["definitions"]["HML"] == "IWD - IWF"
        assert provenance["definitions"]["RMW"] == "QUAL - SPY"
        assert provenance["definitions"]["CMA"] == "XLU - XLI"
        assert provenance["definitions"]["MOM"] == "MTUM - SPY"
        assert provenance["unavailable"] == {}
        assert list(frame.columns) == ["MKT", "SMB", "HML", "RMW", "CMA", "MOM", "RF"]
        # MKT is SPY minus the daily risk-free.
        expected = closes["SPY"].pct_change().iloc[-1] - 0.04 / 252
        assert frame["MKT"].iloc[-1] == pytest.approx(expected)
        assert etf[0] is frame

    def test_etf_proxy_falls_back_to_cross_sectional_momentum(self) -> None:
        rng = np.random.default_rng(20)
        n = 700
        index = _bdates(n)
        symbols = ("SPY", "IWM", "XLF", "XLK", "XLU", "XLI", "XLP", "XLY")
        closes = {
            symbol: pd.Series(
                100.0 * np.cumprod(1.0 + rng.normal(0.0003, 0.01, n)), index=index
            )
            for symbol in symbols
        }
        _, provenance = factors.etf_proxy_factors(closes)
        assert provenance["definitions"]["HML"] == "XLF - XLK"
        assert "cross-sectional" in provenance["definitions"]["MOM"]

    def test_missing_market_symbol_is_reported(self) -> None:
        frame, provenance = factors.etf_proxy_factors({"IWM": pd.Series([1.0, 2.0, 3.0])})
        assert frame.empty
        assert provenance["unavailable"]["MKT"].startswith("SPY closes missing")

    def test_build_factors_falls_back_to_proxies(self, tmp_path: Any) -> None:
        closes, _, _ = self._synthetic(n=800)
        rng = np.random.default_rng(21)
        index = closes.index
        proxies = {
            symbol: pd.Series(
                100.0 * np.cumprod(1.0 + rng.normal(0.0003, 0.01, len(index))), index=index
            )
            for symbol in ("SPY", "IWM", "IWD", "IWF", "QUAL", "XLU", "XLI", "MTUM")
        }
        section = factors.build_factors(
            closes,
            proxy_closes=proxies,
            risk_free_annual=0.04,
            cache_dir=tmp_path / "nothing",
            allow_download=False,
        )
        assert section["model"] == "etf_proxy"
        assert section["source"]["definitions"]["SMB"] == "IWM - SPY"
        assert section["windows"]["1y"]["error"] is None

    def test_joins_across_sources_with_mismatched_timestamps(self) -> None:
        """Massive stamps daily bars at 04:00; Ken French publishes plain dates."""
        closes, frame, betas = self._synthetic(n=800)
        closes.index = pd.DatetimeIndex(closes.index.to_numpy() + np.timedelta64(4, "h"))
        section = factors.build_factors(closes, factors=frame)
        assert section["error"] is None
        window = section["windows"]["1y"]
        assert window["error"] is None
        assert window["n"] == 252
        assert window["betas"]["MKT"] == pytest.approx(betas["MKT"], abs=0.15)

    def test_normalize_daily_index_snaps_to_midnight(self) -> None:
        index = pd.DatetimeIndex(["2024-01-02 04:00", "2024-01-03 04:00", "2024-01-03 09:30"])
        series = pd.Series([1.0, 2.0, 3.0], index=index)
        out = factors.normalize_daily_index(series)
        assert list(out.index) == [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")]
        assert out.iloc[-1] == 3.0  # duplicates keep the last observation

    def test_build_factors_without_any_source_reports_an_error(self, tmp_path: Any) -> None:
        closes, _, _ = self._synthetic(n=300)
        section = factors.build_factors(
            closes, cache_dir=tmp_path / "nothing", allow_download=False
        )
        assert section["error"]
        assert section["model"] is None


# --------------------------------------------------------------------------
# scenarios.py
# --------------------------------------------------------------------------


def _regimes_section(closes: pd.Series) -> dict[str, Any]:
    return regimes.build_regimes(closes, ticker_close=closes, trained_on="SYNTH")


def _seasonality_section() -> dict[str, Any]:
    forward = {
        label: {
            "mean": 0.01 * factor,
            "median": 0.008 * factor,
            "n": 12,
            "hit_rate": 0.6,
            "p10": -0.05 * factor,
            "p90": 0.08 * factor,
        }
        for label, factor in (("1m", 1), ("2m", 2), ("3m", 3), ("6m", 5), ("12m", 8), ("18m", 10))
    }
    return {
        "month": 9,
        "month_label": "September",
        "ticker": {
            "this_month": {"10y": {"mean": 0.01, "hit_rate": 0.7, "n": 10}},
            "forward": forward,
        },
    }


def _macro_section() -> dict[str, Any]:
    return {
        "hy_spread": {"change_3m": -0.2, "change_mode": "diff"},
        "vix": {"change_3m": -3.0, "change_mode": "diff"},
        "dollar": {"change_3m": 0.02, "change_mode": "pct"},
        "curve_shape": {"2s10s": 0.6},
    }


class TestScenarioComponents:
    def test_missing_sections_are_reported_not_invented(self) -> None:
        components = scenarios.component_forecasts()
        assert set(components) == {
            "seasonality",
            "regime",
            "factors",
            "spectral",
            "fundamentals",
            "macro",
        }
        for component in components.values():
            assert component["available"] is False
            assert component["reason"]
            assert all(value is None for value in component["expected_return"].values())

    def test_seasonality_component_uses_p10_p90_for_spread(self) -> None:
        components = scenarios.component_forecasts(seasonality=_seasonality_section())
        seasonal = components["seasonality"]
        assert seasonal["available"] is True
        assert seasonal["expected_return"]["1m"] == pytest.approx(0.01)
        assert seasonal["sigma"]["1m"] == pytest.approx((0.08 + 0.05) / (2 * 1.2815515655446004))

    def test_regime_component_propagates_the_transition_matrix(self, three_regime: Any) -> None:
        closes, _, _ = three_regime
        section = _regimes_section(closes)
        components = scenarios.component_forecasts(regimes=section, realized_vol_annual=0.2)
        regime_component = components["regime"]
        assert regime_component["available"] is True
        assert regime_component["expected_return"]["1m"] is not None
        # Sigma must grow with the horizon.
        assert (
            regime_component["sigma"]["1m"]
            < regime_component["sigma"]["6m"]
            < regime_component["sigma"]["18m"]
        )

    def test_factors_component_prices_exposures_with_realised_premia(self) -> None:
        closes, frame, _ = TestFactorSection._synthetic()
        section = factors.build_factors(closes, factors=frame)
        components = scenarios.component_forecasts(factors=section, realized_vol_annual=0.3)
        factor_component = components["factors"]
        assert factor_component["available"] is True
        assert factor_component["window_used"] in {"5y", "10y", "3y", "1y"}
        assert math.isfinite(factor_component["expected_return"]["12m"])

    def test_macro_component_reads_change_mode(self) -> None:
        components = scenarios.component_forecasts(macro=_macro_section())
        macro_component = components["macro"]
        assert macro_component["available"] is True
        # Every input above is risk-on except a firmer dollar.
        assert macro_component["contributions"]["hy_spread"] > 0
        assert macro_component["contributions"]["vix"] > 0
        assert macro_component["contributions"]["dollar"] < 0
        assert macro_component["contributions"]["curve"] > 0

    def test_fundamentals_stage_tilt(self) -> None:
        components = scenarios.component_forecasts(
            fundamentals={"stage": {"label": "declining"}, "growth": {"revenue_yoy": -0.2}}
        )
        assert components["fundamentals"]["annual_tilt"] < 0
        assert components["fundamentals"]["expected_return"]["12m"] < 0


class TestMixture:
    @staticmethod
    def _components() -> dict[str, dict[str, Any]]:
        horizons = scenarios.HORIZONS
        return {
            "regime": {
                "component": "regime",
                "available": True,
                "expected_return": {label: 0.01 * days / 21 for label, days in horizons.items()},
                "sigma": {label: 0.05 * math.sqrt(days / 21) for label, days in horizons.items()},
            },
            "spectral": {
                "component": "spectral",
                "available": True,
                "expected_return": {label: -0.005 * days / 21 for label, days in horizons.items()},
                "sigma": {label: 0.07 * math.sqrt(days / 21) for label, days in horizons.items()},
            },
            "macro": {"component": "macro", "available": False, "reason": "not supplied"},
        }

    def test_probabilities_sum_to_one_and_percentiles_are_ordered(self) -> None:
        mixture = scenarios.mix(
            self._components(), {"regime": 0.6, "spectral": 0.4}, current_price=100.0
        )
        for label in scenarios.HORIZONS:
            total = sum(
                mixture["cases"][case]["horizons"][label]["probability"]
                for case in ("bull", "neutral", "bear")
            )
            assert total == pytest.approx(1.0, abs=1e-9)
            for case in ("bull", "neutral", "bear"):
                block = mixture["cases"][case]["horizons"][label]
                assert block["p10"] < block["p50"] < block["p90"]
                assert block["price_p10"] < block["price_p50"] < block["price_p90"]
                # `expected_return` is the truncated conditional *mean*; the
                # median is reported separately.
                assert block["median_return"] == pytest.approx(block["p50"])
                assert block["p10"] <= block["expected_return"] <= block["p90"]
            # The tails are asymmetric about their medians: the bear case's mean
            # sits below its median and the bull case's above.
            bear = mixture["cases"]["bear"]["horizons"][label]
            bull = mixture["cases"]["bull"]["horizons"][label]
            assert bear["expected_return"] < bear["median_return"]
            assert bull["expected_return"] > bull["median_return"]

    def test_cases_are_ordered_bear_below_neutral_below_bull(self) -> None:
        mixture = scenarios.mix(
            self._components(), {"regime": 0.6, "spectral": 0.4}, current_price=100.0
        )
        for label in scenarios.HORIZONS:
            bear = mixture["cases"]["bear"]["horizons"][label]["p50"]
            neutral = mixture["cases"]["neutral"]["horizons"][label]["p50"]
            bull = mixture["cases"]["bull"]["horizons"][label]["p50"]
            assert bear < neutral < bull

    def test_mixture_moments_match_the_analytic_values(self) -> None:
        components = {
            "a": {
                "available": True,
                "expected_return": {"1m": 0.10},
                "sigma": {"1m": 0.20},
            },
            "b": {
                "available": True,
                "expected_return": {"1m": -0.10},
                "sigma": {"1m": 0.20},
            },
        }
        mixture = scenarios.mix(
            components, {"a": 0.5, "b": 0.5}, horizons={"1m": 21}, current_price=None
        )
        distribution = mixture["distribution"]["1m"]
        # The move to log space is moment-preserving: each component is matched
        # onto a lognormal with the same mean and sd, so the mixture's
        # simple-return mean and sd are exactly what the Gaussian mixture gave.
        assert distribution["mean"] == pytest.approx(0.0, abs=1e-12)
        assert distribution["std"] == pytest.approx(math.sqrt(0.2**2 + 0.1**2))
        assert distribution["n_components"] == 2
        assert distribution["return_space"] == "log"
        # The shape does change, and must: a lognormal mixture is right-skewed,
        # where the old Gaussian-on-simple-returns mixture was symmetric — and
        # unbounded below, which is the defect this replaced.
        assert distribution["skew"] > 0.0
        assert distribution["mean_log"] < distribution["mean"]
        assert distribution["geometric_mean_return"] < distribution["mean"]

    def test_the_mixture_lives_in_log_space_and_matches_the_moments(self) -> None:
        """`to_log_space` must reproduce both moments it was handed."""
        for mu, sigma in ((0.10, 0.20), (-0.30, 0.90), (2.00, 3.00)):
            converted = scenarios.to_log_space(mu, sigma)
            assert converted is not None
            m, s = converted
            assert math.exp(m + s * s / 2.0) - 1.0 == pytest.approx(mu)
            variance = (math.exp(s * s) - 1.0) * math.exp(2.0 * m + s * s)
            assert math.sqrt(variance) == pytest.approx(sigma)
        # A forecast at or below -100% has no lognormal image, and neither does
        # a zero spread.
        assert scenarios.to_log_space(-1.0, 0.5) is None
        assert scenarios.to_log_space(-1.5, 0.5) is None
        assert scenarios.to_log_space(0.1, 0.0) is None

    def test_a_high_volatility_name_can_never_print_a_return_below_minus_100(self) -> None:
        """The MU defect: 80% annualised vol produced a -176% 12-month bear case.

        A Gaussian on the *simple* return has no floor. In log space the bear
        tail is bounded, every price percentile is positive and monotone, and
        `price_p50` is exactly `spot * exp(median_log)`.
        """
        horizons = {"3m": 63, "6m": 126, "12m": 252, "18m": 378}
        annual_vol = 0.80
        components = {
            name: {
                "available": True,
                "expected_return": {
                    label: math.expm1(math.log1p(drift) * days / 252.0)
                    for label, days in horizons.items()
                },
                "sigma": {
                    label: annual_vol * width * math.sqrt(days / 252.0)
                    for label, days in horizons.items()
                },
            }
            for name, drift, width in (
                ("regime", 0.70, 1.0),
                ("spectral", 0.02, 1.6),
                ("macro", 0.01, 2.0),
            )
        }
        spot = 929.0
        mixture = scenarios.mix(
            components,
            {"regime": 0.5, "spectral": 0.3, "macro": 0.2},
            horizons=horizons,
            current_price=spot,
        )

        assert mixture["return_space"] == "log"
        assert mixture["mixture_parts_space"] == "log"
        for label in horizons:
            probabilities = 0.0
            for case in ("bear", "neutral", "bull"):
                block = mixture["cases"][case]["horizons"][label]
                probabilities += block["probability"]
                for key in ("expected_return", "p10", "p50", "p90", "median_return"):
                    assert block[key] > -1.0, f"{case} {label} {key}"
                assert 0.0 < block["price_p10"] < block["price_p50"] < block["price_p90"]
                assert block["price_p50"] == pytest.approx(
                    spot * math.exp(block["median_log_return"])
                )
                assert block["p10"] < block["p50"] < block["p90"]
            assert probabilities == pytest.approx(1.0)
            bear = mixture["cases"]["bear"]["horizons"][label]["expected_return"]
            neutral = mixture["cases"]["neutral"]["horizons"][label]["expected_return"]
            bull = mixture["cases"]["bull"]["horizons"][label]["expected_return"]
            assert bull > neutral > bear > -1.0

        zone = scenarios.entry_zone(mixture, spot, horizon="12m")
        assert 0.0 < zone["bargain_below"] < zone["fair_value"] < zone["expensive_above"]
        assert zone["return_space"] == "log"

    def test_zero_weight_components_are_excluded(self) -> None:
        mixture = scenarios.mix(
            self._components(), {"regime": 1.0, "spectral": 0.0}, current_price=100.0
        )
        assert mixture["effective_weights"]["1m"] == {"regime": 1.0}
        assert mixture["unavailable_components"]["macro"] == "not supplied"

    def test_mixture_parts_are_exposed_and_drive_the_entry_zone(self) -> None:
        mixture = scenarios.mix(
            self._components(), {"regime": 0.6, "spectral": 0.4}, current_price=100.0
        )
        parts = mixture["mixture_parts"]["6m"]
        assert len(parts) == 2
        assert sum(weight for weight, _, _ in parts) == pytest.approx(1.0)

        zone = scenarios.entry_zone(mixture, 100.0, horizon="6m")
        assert zone["bargain_below"] < zone["fair_value"] < zone["expensive_above"]
        # The band must come from the exact parts, not the case-block fallback.
        stripped = {key: value for key, value in mixture.items() if key != "mixture_parts"}
        approximate = scenarios.entry_zone(stripped, 100.0, horizon="6m")
        assert approximate["fair_value"] == pytest.approx(zone["fair_value"], rel=0.05)

    def test_no_usable_component_yields_nulls_not_zeros(self) -> None:
        mixture = scenarios.mix({"macro": {"available": False, "reason": "x"}}, {"macro": 1.0})
        assert mixture["distribution"]["1m"]["mean"] is None
        assert mixture["cases"]["bull"]["horizons"]["1m"]["probability"] is None


class TestWeighting:
    def test_weights_sum_to_one_without_any_history(self) -> None:
        result = scenarios.walk_forward_weights(pd.DataFrame(), pd.Series(dtype=float))
        assert sum(result["weights"].values()) == pytest.approx(1.0)
        assert result["evidence"]["reason"] == "no prediction history supplied"
        assert result["weights"] == pytest.approx(
            {
                name: value / sum(scenarios.PRIOR_WEIGHTS.values())
                for name, value in scenarios.PRIOR_WEIGHTS.items()
            }
        )

    def test_a_skilful_component_gains_weight_over_a_useless_one(self) -> None:
        rng = np.random.default_rng(22)
        n = 900
        index = _bdates(n)
        realized = pd.Series(rng.normal(0.01, 0.05, n), index=index)
        predictions = pd.DataFrame(
            {
                "regime": realized * 0.9 + rng.normal(0.0, 0.005, n),
                "seasonality": rng.normal(0.05, 0.05, n),
            },
            index=index,
        )
        result = scenarios.walk_forward_weights(predictions, realized, holdout_days=252)
        weights = result["weights"]
        evidence = result["evidence"]

        assert evidence["components"]["regime"]["skill"] > 0.8
        assert evidence["components"]["seasonality"]["skill"] < 0
        assert weights["regime"] > weights["seasonality"]
        assert sum(weights.values()) == pytest.approx(1.0)
        assert evidence["n_test"] == 252

    def test_default_prediction_history_has_no_look_ahead(self, three_regime: Any) -> None:
        closes, _, _ = three_regime
        fit = regimes.fit_regime_model(closes)
        labels = regimes.regime_state_series(fit)
        predictions, realized = scenarios.default_prediction_history(
            closes, regime_labels=labels, horizon_days=21
        )
        assert not predictions.empty
        assert "seasonality" in predictions.columns
        assert "regime" in predictions.columns
        # The first rows cannot have a prediction: nothing has settled yet.
        assert predictions["seasonality"].iloc[0] != predictions["seasonality"].iloc[0]  # NaN
        assert realized.index.equals(predictions.index)
        # realized[t] must equal the actual forward 21-day return.
        stamp = predictions.index[500]
        position = closes.index.get_loc(stamp)
        expected = closes.iloc[position + 21] / closes.iloc[position] - 1.0
        assert realized.loc[stamp] == pytest.approx(expected)

    def test_short_history_yields_an_empty_prediction_frame(self) -> None:
        closes = _closes_from_returns(np.full(100, 0.001))
        predictions, realized = scenarios.default_prediction_history(closes)
        assert predictions.empty
        assert realized.empty

    def test_relative_fallback_when_nothing_has_absolute_skill(self) -> None:
        rng = np.random.default_rng(31)
        n = 900
        index = _bdates(n)
        realized = pd.Series(rng.normal(0.0, 0.05, n), index=index)
        predictions = pd.DataFrame(
            {
                # Both are useless, but "mild" is much less wrong than "wild".
                "regime": rng.normal(0.0, 0.02, n),
                "macro": rng.normal(0.0, 0.30, n),
            },
            index=index,
        )
        result = scenarios.walk_forward_weights(predictions, realized, holdout_days=252)
        evidence = result["evidence"]
        assert all(entry["skill"] < 0 for entry in evidence["components"].values())
        assert evidence["fallback"] == "relative_skill_ranking"
        assert "no component beat the naive constant forecast" in evidence["reason"]
        assert result["weights"]["regime"] > result["weights"]["macro"]
        assert sum(result["weights"].values()) == pytest.approx(1.0)

    def test_arbitrary_component_names_get_a_flat_prior(self) -> None:
        result = scenarios.walk_forward_weights(
            pd.DataFrame(), pd.Series(dtype=float), components=["SPY", "SOXX", "entropy"]
        )
        assert result["weights"] == pytest.approx(
            {"SPY": 1 / 3, "SOXX": 1 / 3, "entropy": 1 / 3}
        )

    def test_signal_prediction_history_is_strictly_backward_looking(self) -> None:
        rng = np.random.default_rng(32)
        n = 1200
        index = _bdates(n)
        # A signal that genuinely predicts the next 21 days.
        shock = rng.normal(0.0, 0.01, n)
        closes = _closes_from_returns(shock)
        forward = closes.shift(-21) / closes - 1.0
        signals = pd.DataFrame(
            {"oracle": forward.fillna(0.0) + rng.normal(0.0, 0.002, n)}, index=index
        )
        predictions, realized = scenarios.signal_prediction_history(signals, closes)

        assert list(predictions.columns) == ["oracle"]
        assert predictions.index.equals(realized.index)
        # Nothing can be predicted before min_train settled pairs exist.
        assert predictions["oracle"].iloc[:252].isna().all()
        assert predictions["oracle"].notna().sum() > 500
        # And it should have learned the relationship out of sample.
        usable = pd.concat([predictions["oracle"], realized], axis=1).dropna()
        assert float(usable.corr().iloc[0, 1]) > 0.8

    def test_signal_prediction_history_needs_enough_history(self) -> None:
        closes = _closes_from_returns(np.full(200, 0.001))
        predictions, realized = scenarios.signal_prediction_history(
            pd.DataFrame({"a": np.arange(200.0)}, index=closes.index), closes
        )
        assert predictions.empty
        assert realized.empty

    def test_make_weight_fn_drives_the_load_bearing_test(self) -> None:
        rng = np.random.default_rng(23)
        n = 900
        index = _bdates(n)
        realized = pd.Series(rng.normal(0.01, 0.05, n), index=index)
        predictions = pd.DataFrame(
            {
                "regime": realized * 0.9 + rng.normal(0.0, 0.005, n),
                "seasonality": rng.normal(0.05, 0.05, n),
            },
            index=index,
        )
        weight_fn = scenarios.make_weight_fn(predictions, realized)

        # The callback is a real intervention on the weighting, not a formula:
        # with the skilful component present it earns weight above its prior;
        # drop it and the weights collapse back to the prior.
        full = weight_fn(["regime", "seasonality"])
        without_regime = weight_fn(["seasonality"])
        prior_share = scenarios.PRIOR_WEIGHTS["regime"] / sum(scenarios.PRIOR_WEIGHTS.values())
        assert full["regime"] > prior_share
        # A dropped signal genuinely leaves the weight vector. Passing the full
        # component list through used to hand it its prior weight back, so the
        # leave-one-out delta was structurally zero.
        assert "regime" not in without_regime
        assert without_regime["seasonality"] == pytest.approx(1.0)

        rows = eigen.load_bearing_test(["regime", "seasonality"], weight_fn, threshold=0.02)
        by_name = {row["signal"]: row for row in rows}
        assert by_name["regime"]["load_bearing"] is True
        assert by_name["regime"]["weight_delta_if_removed"] > 0.05


class TestBuildScenarios:
    def test_end_to_end_on_synthetic_data(self, three_regime: Any) -> None:
        closes, _, _ = three_regime
        regime_section = _regimes_section(closes)
        fit = regimes.fit_regime_model(closes)
        labels = regimes.regime_state_series(fit)
        entropy_section = entropy.build_entropy(closes)
        spectral_section = spectral.build_spectral(closes)
        factor_closes, factor_frame, _ = TestFactorSection._synthetic(n=len(closes))
        factor_frame.index = closes.index
        factors_section = factors.build_factors(closes, factors=factor_frame)

        section = scenarios.build_scenarios(
            close=closes,
            seasonality=_seasonality_section(),
            regimes=regime_section,
            factors=factors_section,
            spectral=spectral_section,
            fundamentals={"stage": {"label": "growing"}, "growth": {"revenue_yoy": 0.2}},
            macro=_macro_section(),
            impact_weights={
                "SPY": {"weight": 0.6, "explained_variance_share": 0.45},
                "QQQ": {"weight": 0.4, "explained_variance_share": 0.30},
            },
            entropy=entropy_section,
            regime_label_series=labels,
            ticker="SYNTH",
            month_label="September",
        )

        assert section["error"] is None
        assert section["method"] == "weighted_mixture_of_shrunk_components"
        assert sum(section["weights"].values()) == pytest.approx(1.0)
        assert set(section["cases"]) == {"bull", "neutral", "bear"}
        assert sum(case["probability"] for case in section["cases"].values()) == pytest.approx(
            1.0, abs=1e-9
        )
        for case in section["cases"].values():
            assert "not investment advice" in case["narrative"].lower()
            assert set(case["horizons"]) == set(scenarios.HORIZONS)

        entry = section["entry"]
        assert entry["bargain_below"] < entry["fair_value"] < entry["expensive_above"]
        assert entry["current_price"] == pytest.approx(float(closes.iloc[-1]))
        assert entry["current_vs_fair"] is not None

        assert section["timing"]["this_month"] in {"good", "neutral", "bad"}
        assert section["timing"]["reason"]
        symbols = {signal["symbol"] for signal in section["watch_signals"]}
        assert {"SPY", "QQQ"} <= symbols

        for label in scenarios.HORIZONS:
            distribution = section["distribution"][label]
            assert distribution["std"] > 0
            assert math.isfinite(distribution["skew"])
            parts = section["mixture_parts"][label]
            assert parts
            assert sum(weight for weight, _, _ in parts) == pytest.approx(1.0)

        # The whole section must survive a strict JSON round-trip: no NaN, no inf,
        # no numpy scalars.
        assert (
            json.loads(json.dumps(section, allow_nan=False))["method"]
            == "weighted_mixture_of_shrunk_components"
        )

    def test_survives_every_section_being_absent(self) -> None:
        closes = _closes_from_returns(np.full(300, 0.0005))
        section = scenarios.build_scenarios(close=closes)
        assert section["error"] == "no component produced a forecast"
        assert sum(section["weights"].values()) == pytest.approx(1.0)
        assert section["entry"]["reason"]
        assert section["timing"]["this_month"] == "unknown"

    def test_entry_zone_needs_a_price(self) -> None:
        mixture = scenarios.mix(
            TestMixture._components(), {"regime": 1.0}, current_price=None
        )
        zone = scenarios.entry_zone(mixture, None)
        assert zone["reason"] == "no current price"


# --------------------------------------------------------------------------
# Live smoke test (opt-in)
# --------------------------------------------------------------------------


@pytest.mark.skipif(os.getenv("PRISM_LIVE") != "1", reason="set PRISM_LIVE=1 for live data")
def test_live_smoke_against_massive() -> None:  # pragma: no cover - network
    from datetime import date, timedelta

    from app.market_data import MarketDataClient

    client = MarketDataClient()
    end = date.today()
    start = end - timedelta(days=365 * 12)

    def closes(symbol: str) -> pd.Series:
        history = client.get_history(symbol, start=start, end=end, interval="1d")
        column = "Adj Close" if "Adj Close" in history.data.columns else "Close"
        return history.data[column].dropna()

    spy = closes("SPY")
    nvda = closes("NVDA")

    regime_section = regimes.build_regimes(spy, ticker_close=nvda)
    assert regime_section["error"] is None
    by_label = {state["label"]: state for state in regime_section["states"]}
    assert by_label["bull"]["mean_daily_return"] > by_label["bear"]["mean_daily_return"]
    assert by_label["bear"]["volatility_annualized"] == max(
        state["volatility_annualized"] for state in regime_section["states"]
    )

    assert entropy.build_entropy(nvda)["error"] is None
    assert spectral.build_spectral(nvda)["error"] is None
    factor_section = factors.build_factors(nvda)
    assert factor_section["error"] is None
    assert factor_section["windows"]["1y"]["betas"]["MKT"] > 0.5
