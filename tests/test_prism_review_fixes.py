"""Regression tests for the adversarial-review findings on the Prism engine.

Each test names the defect it pins down. They are deliberately grouped by the
property being defended rather than by module, because several fixes span two
files (the engine decides what to pass, the quant module decides what to do with
it) and the property only holds when both halves are right.
"""

from __future__ import annotations

import base64
import json
import math
import zlib
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.main import create_app
from app.prism import (
    eigen as eigen_module,
)
from app.prism import (
    entropy as entropy_module,
)
from app.prism import (
    export as export_module,
)
from app.prism import (
    hmm as hmm_module,
)
from app.prism import (
    macro as macro_module,
)
from app.prism import (
    memo as memo_module,
)
from app.prism import (
    regimes as regimes_module,
)
from app.prism import (
    relational as relational_module,
)
from app.prism import (
    scenarios as scenarios_module,
)
from app.prism import (
    spectral as spectral_module,
)
from app.prism import (
    store as store_module,
)
from app.prism.engine import _resolve_as_of
from app.prism.routes import clean_as_of

SECRET = "SUPERSECRETFREDKEY123"


def _bdays(n: int, start: str = "2016-01-04") -> pd.DatetimeIndex:
    return pd.bdate_range(start=start, periods=n)


def _trending_closes(n: int = 2600, *, seed: int = 7) -> pd.Series:
    rng = np.random.default_rng(seed)
    index = _bdays(n)
    steps = rng.normal(0.0006, 0.018, n)
    return pd.Series(100.0 * np.exp(np.cumsum(steps)), index=index, name="close")


# ---------------------------------------------------------------------------
# Finding 13 — the FRED API key must never reach the packet.
# ---------------------------------------------------------------------------


class TestFredKeyNeverLeaks:
    def test_scrubber_redacts_every_secret_query_parameter(self) -> None:
        message = (
            "HTTPSConnectionPool(host='api.stlouisfed.org', port=443): Max retries "
            f"exceeded with url: /fred/series/observations?series_id=DGS10&api_key={SECRET}"
            "&file_type=json"
        )

        scrubbed = macro_module.scrub_secrets(message)

        assert SECRET not in scrubbed
        assert "api_key=***" in scrubbed
        assert "series_id=DGS10" in scrubbed  # only the secret is redacted

    def test_transport_failure_message_carries_no_key(self) -> None:
        client = macro_module.FredClient(
            SECRET, base_url="https://127.0.0.1:1/fred", timeout=0.05, max_retries=0
        )

        with pytest.raises(macro_module.FredError) as excinfo:
            client.get_series("DGS10")

        assert SECRET not in str(excinfo.value)

    def test_unreachable_fred_leaves_no_key_anywhere_in_the_section(self) -> None:
        client = macro_module.FredClient(
            SECRET, base_url="https://127.0.0.1:1/fred", timeout=0.05, max_retries=0
        )

        section = macro_module.build_macro_section(fred=client, as_of="2026-09-01", years=2)

        rendered = json.dumps(section, default=str)
        assert SECRET not in rendered
        # The failure must still be reported honestly, just without the key.
        assert section["unavailable"], "an unreachable FRED must be reported"


# ---------------------------------------------------------------------------
# Finding 14 — an unvalidated as_of poisons the store and the export header.
# ---------------------------------------------------------------------------


class TestAsOfValidation:
    @pytest.mark.parametrize(
        "value",
        ['a"\r\nX-Evil: 1', "../../etc/passwd", "9999-99-99", "2026-13-01", "latest"],
    )
    def test_route_rejects_anything_that_is_not_an_iso_date(self, value: str) -> None:
        with pytest.raises(ValueError, match="ISO date"):
            clean_as_of(value)

    def test_route_accepts_a_real_date_and_passes_none_through(self) -> None:
        assert clean_as_of("2026-06-30") == "2026-06-30"
        assert clean_as_of(None) is None
        assert clean_as_of("  ") is None

    @pytest.mark.parametrize("value", ['a"\r\nX-Evil: 1', "../../etc/passwd", "9999-99-99"])
    def test_engine_refuses_rather_than_truncating(self, value: str) -> None:
        with pytest.raises(Exception, match="ISO date"):
            _resolve_as_of(value)

    def test_engine_still_accepts_dates_and_iso_strings(self) -> None:
        assert _resolve_as_of("2026-06-30") == "2026-06-30"
        assert _resolve_as_of(date(2026, 6, 30)) == "2026-06-30"

    def test_a_junk_filename_cannot_shadow_the_real_packet(self, tmp_path: Path) -> None:
        store = store_module.PrismStore(base_dir=tmp_path)
        real = {"ticker": "NVDA", "as_of": "2026-09-01", "marker": "REAL"}
        store.save_packet(real)
        # Whatever put it there, a stem that is not an ISO date is not a packet.
        poison = store.packet_dir("NVDA") / "zzzz-poison.json"
        poison.write_text(json.dumps({"ticker": "NVDA", "marker": "POISON"}), encoding="utf-8")

        loaded = store.load_packet("NVDA")

        assert loaded is not None
        assert loaded["marker"] == "REAL"

    def test_export_filename_is_header_safe(self) -> None:
        packet = {"ticker": 'NV"DA', "as_of": 'a"\r\nX-Evil: 1', "memo": None}

        _body, _content_type, filename = export_module.export_packet(packet, "json")

        assert "\r" not in filename and "\n" not in filename and '"' not in filename
        assert filename.endswith(".json")


# ---------------------------------------------------------------------------
# Finding 5 — cases carry the conditional mean, not the conditional median.
# ---------------------------------------------------------------------------


class TestTruncatedConditionalMean:
    @staticmethod
    def _numeric_mean(
        parts: list[tuple[float, float, float]], lo: float | None, hi: float | None
    ) -> float:
        low = lo if lo is not None else -6.0
        high = hi if hi is not None else 6.0
        grid = np.linspace(low, high, 400_001)
        density = np.zeros_like(grid)
        for weight, mu, sigma in parts:
            density += weight * np.exp(-0.5 * ((grid - mu) / sigma) ** 2) / (
                sigma * math.sqrt(2.0 * math.pi)
            )
        mass = float(np.trapezoid(density, grid))
        return float(np.trapezoid(grid * density, grid) / mass)

    def test_matches_numerical_integration_on_both_tails(self) -> None:
        parts = [(0.5, 0.02, 0.20), (0.3, 0.35, 0.55), (0.2, -0.05, 0.10)]
        for lo, hi in ((None, -0.15), (-0.15, 0.30), (0.30, None)):
            analytic = scenarios_module.truncated_mixture_mean(parts, lower=lo, upper=hi)
            assert analytic is not None
            assert analytic == pytest.approx(self._numeric_mean(parts, lo, hi), abs=2e-4)

    def test_bear_case_mean_is_below_its_median(self) -> None:
        # The property the old code got wrong: for a truncated lower tail the mean
        # sits materially below the median, so reporting the median as
        # `expected_return` understated the drawdown.
        parts = [(0.6, 0.20, 0.45), (0.4, 0.05, 0.25)]
        block = scenarios_module._case_block(
            parts,
            lower=None,
            upper=-0.05,
            probability=0.2,
            current_price=100.0,
            search_lo=-3.0,
            search_hi=3.0,
        )

        assert block["expected_return"] < block["median_return"]
        assert block["median_return"] == pytest.approx(block["p50"])


# ---------------------------------------------------------------------------
# Finding 1 — the factors component must not be the ticker's trailing mean.
# ---------------------------------------------------------------------------


def _factors_section(*, with_premia: bool) -> dict[str, Any]:
    window = {
        "alpha_daily": 0.0015,
        "alpha_annual": 0.0015 * 252,
        "betas": {"MKT": 1.6, "SMB": -0.2, "MOM": 0.3},
        "factor_means": {"MKT": 0.0007, "SMB": 0.0001, "MOM": 0.0002},
        "r2": 0.61,
        "residual_vol_daily": 0.02,
        "residual_vol_annual": 0.02 * math.sqrt(252),
        "n": 1260,
        "error": None,
        "start": "2021-06-30",
        "end": "2026-06-30",
    }
    section: dict[str, Any] = {
        "model": "fama_french_5_mom",
        "windows": {"5y": window},
        "error": None,
    }
    if with_premia:
        section["premia"] = {
            "daily": {"MKT": 0.0003, "SMB": 0.00002, "MOM": 0.00005},
            "source": "full_sample_factor_means",
            "start": "1963-07-01",
            "end": "2026-06-30",
            "n": 15_800,
        }
    return section


class TestFactorComponentIsNotAnIdentity:
    def test_alpha_is_excluded_and_premia_come_from_outside_the_window(self) -> None:
        component = scenarios_module._factors_component(
            _factors_section(with_premia=True), {"12m": 252}, {"12m": 0.4}
        )

        assert component["available"] is True
        assert component["premia_source"] == "full_sample_factor_means"
        # beta . premium only — the in-sample alpha is reported, never summed in.
        expected_daily = 1.6 * 0.0003 + -0.2 * 0.00002 + 0.3 * 0.00005
        assert component["beta_premium_daily_return"] == pytest.approx(expected_daily)
        assert component["alpha_component"]["alpha_daily"] == pytest.approx(0.0015)
        assert component["expected_return"]["12m"] == pytest.approx(
            math.expm1(math.log1p(expected_daily) * 252)
        )

    def test_the_fitted_windows_own_means_are_never_used_as_premia(self) -> None:
        # Without out-of-window premia the component must refuse rather than
        # reproduce alpha + sum(beta_i * xbar_i), which by the OLS normal
        # equations equals the ticker's own trailing mean excess return.
        component = scenarios_module._factors_component(
            _factors_section(with_premia=False), {"12m": 252}, {"12m": 0.4}
        )

        assert component["available"] is False
        assert "OLS identity" in str(component["reason"])

    def test_confidence_is_not_the_in_sample_r_squared(self) -> None:
        component = scenarios_module._factors_component(
            _factors_section(with_premia=True), {"12m": 252}, {"12m": 0.4}
        )

        assert component["confidence"] != pytest.approx(0.61)
        assert component["in_sample_r2"] == pytest.approx(0.61)
        assert "out of sample" in component["confidence_basis"]

    def test_build_factors_publishes_out_of_sample_premia_and_staleness(self) -> None:
        n = 900
        index = _bdays(n)
        rng = np.random.default_rng(11)
        frame = pd.DataFrame(
            {
                "MKT": rng.normal(0.0004, 0.01, n),
                "SMB": rng.normal(0.0, 0.006, n),
                "HML": rng.normal(0.0, 0.006, n),
                "RMW": rng.normal(0.0, 0.005, n),
                "CMA": rng.normal(0.0, 0.005, n),
                "MOM": rng.normal(0.0, 0.007, n),
                "RF": np.full(n, 0.00008),
            },
            index=index,
        )
        # Prices run 40 sessions past the last factor date: the published lag.
        closes = pd.Series(
            100.0 * np.exp(np.cumsum(rng.normal(0.0008, 0.02, n + 40))),
            index=_bdays(n + 40),
        )

        section = factors_build(closes, frame)

        assert section["premia"]["source"] == "full_sample_factor_means"
        assert section["premia"]["daily"]["MKT"] == pytest.approx(float(frame["MKT"].mean()))
        assert section["stale_days"] > 30
        assert section["as_of"] == str(index[-1].date())


def factors_build(closes: pd.Series, frame: pd.DataFrame) -> dict[str, Any]:
    from app.prism.factors import build_factors

    return build_factors(closes, factors=frame, allow_download=False)


# ---------------------------------------------------------------------------
# Finding 2 — regime-conditional stats must be forward and causally decoded.
# ---------------------------------------------------------------------------


class TestRegimeStatsAreForward:
    def test_forward_alignment_answers_what_happens_after_a_label(self) -> None:
        index = _bdays(12)
        # A ticker that falls hard on "bear" days and rises the day after. The
        # first close carries no return, so the labels start one day later.
        returns = np.array([-0.05, 0.04, -0.05, 0.04, -0.05, 0.04, 0.01, 0.01, 0.01, 0.01, 0.01])
        closes = pd.Series(
            np.concatenate([[100.0], 100.0 * np.cumprod(1.0 + returns)]), index=index
        )
        labels = pd.Series(
            ["bull", "bear", "bull", "bear", "bull", "bear"] + ["bull"] * 6, index=index
        )

        contemporaneous = regimes_module.ticker_stats_by_regime(
            closes, labels, min_observations=2, forward=False
        )
        forward = regimes_module.ticker_stats_by_regime(
            closes, labels, min_observations=2, forward=True
        )

        assert contemporaneous["bear"]["mean_daily"] < 0
        assert contemporaneous["bear"]["alignment"] == "same_day"
        # The same labels, read forward, say the opposite — which is exactly why
        # the contemporaneous block must never be consumed as E[r | state].
        assert forward["bear"]["mean_daily"] > 0
        assert forward["bear"]["alignment"] == "next_day"

    def test_build_regimes_publishes_both_blocks_and_uses_forward_for_the_default(
        self,
    ) -> None:
        spy = _trending_closes(1200, seed=3)
        ticker = _trending_closes(1200, seed=4)

        section = regimes_module.build_regimes(spy, ticker, train_window_days=1000)

        assert section["error"] is None
        assert section["ticker_by_regime"] is not None
        assert section["ticker_by_regime_contemporaneous"] is not None
        assert "forward-filtered" in section["ticker_by_regime_basis"]
        for block in section["ticker_by_regime"].values():
            if block.get("mean_daily") is not None:
                assert block["alignment"] == "next_day"

    def test_filtered_posteriors_are_causal(self) -> None:
        rng = np.random.default_rng(19)
        # Overlapping states, so the decoding of an ambiguous day genuinely
        # depends on whether later data is allowed to inform it.
        observations = np.concatenate(
            [rng.normal(-0.4, 1.0, 300), rng.normal(0.4, 1.0, 300)]
        ).reshape(-1, 1)
        model = hmm_module.fit_gaussian_hmm(observations, n_states=2, seed=5)

        prefix = hmm_module.filtered_posteriors(model, observations[:400])
        full = hmm_module.filtered_posteriors(model, observations)

        # Row t of the filtered posterior depends only on rows 0..t, so extending
        # the sequence cannot change any earlier row.
        assert np.allclose(prefix, full[:400], atol=1e-10)
        assert np.allclose(full.sum(axis=1), 1.0)

        # And it is a different decoding from the smoothed one, which is the
        # whole point: the Viterbi path at t is informed by data after t.
        filtered_path = hmm_module.filtered_states(model, observations)
        smoothed_path = hmm_module.viterbi(model, observations)
        assert np.array_equal(filtered_path, np.argmax(full, axis=1))
        assert not np.array_equal(filtered_path, smoothed_path)


# ---------------------------------------------------------------------------
# Finding 3 — the load-bearing test must be able to return a non-zero delta.
# ---------------------------------------------------------------------------


class TestLoadBearingIsMeasurable:
    def test_a_perfect_predictor_is_load_bearing_at_a_monthly_holdout(self) -> None:
        rng = np.random.default_rng(31)
        n = 112
        index = pd.date_range("2017-01-31", periods=n, freq="ME")
        realized = pd.Series(rng.normal(0.01, 0.05, n), index=index)
        predictions = pd.DataFrame(
            {
                "perfect": realized.to_numpy(),
                "noise_a": rng.normal(0.0, 0.05, n),
                "noise_b": rng.normal(0.0, 0.05, n),
            },
            index=index,
        )
        names = list(predictions.columns)
        weight_fn = scenarios_module.make_weight_fn(
            predictions, realized, holdout_days=36, components=names
        )

        rows = eigen_module.load_bearing_test(names, weight_fn, threshold=0.05)
        by_name = {row["signal"]: row for row in rows}

        assert by_name["perfect"]["weight_delta_if_removed"] > 0.0
        assert by_name["perfect"]["load_bearing"] is True
        # `load_bearing` is the interventional quantity: dropping the perfect
        # predictor changes how the survivors are weighted against each other,
        # while dropping either noise column barely does.
        assert by_name["perfect"]["survivor_weight_delta"] > max(
            by_name["noise_a"]["survivor_weight_delta"],
            by_name["noise_b"]["survivor_weight_delta"],
        )

    def test_the_total_delta_alone_would_call_every_signal_load_bearing(self) -> None:
        # A flat-prior weighting: nothing is measured, so no signal changes how
        # the others are weighted — but the raw L1 is 2/n for every one of them.
        index = pd.date_range("2017-01-31", periods=60, freq="ME")
        realized = pd.Series(np.linspace(0.0, 0.1, 60), index=index)
        predictions = pd.DataFrame(
            {name: np.full(60, np.nan) for name in ("a", "b", "c", "d")}, index=index
        )
        weight_fn = scenarios_module.make_weight_fn(
            predictions, realized, holdout_days=12, components=["a", "b", "c", "d"]
        )

        rows = eigen_module.load_bearing_test(["a", "b", "c", "d"], weight_fn, threshold=0.05)

        for row in rows:
            assert row["weight_delta_if_removed"] == pytest.approx(0.5)
            assert row["survivor_weight_delta"] == pytest.approx(0.0, abs=1e-9)
            assert row["load_bearing"] is False

    def test_reason_distinguishes_unscored_from_beaten_by_the_baseline(self) -> None:
        index = pd.date_range("2016-01-31", periods=140, freq="ME")
        realized = pd.Series(np.linspace(0.0, 0.1, 140), index=index)
        column = realized.to_numpy().astype(float).copy()
        column[-36:] = np.nan  # the split happens, but nothing survives in the test slice
        predictions = pd.DataFrame({"a": column}, index=index)

        result = scenarios_module.walk_forward_weights(
            predictions, realized, holdout_days=36, components=["a"]
        )

        assert result["evidence"]["components"]["a"]["skill"] is None
        assert "could be scored" in result["evidence"]["reason"]


# ---------------------------------------------------------------------------
# Finding 4 — the signal ranking must not be the input column order.
# ---------------------------------------------------------------------------


class TestSignalRanking:
    def _monthly_frame(self) -> tuple[pd.DataFrame, pd.Series]:
        rng = np.random.default_rng(41)
        n = 40
        index = pd.date_range("2022-01-31", periods=n, freq="ME")
        target = pd.Series(rng.normal(0.01, 0.05, n), index=index)
        signals = pd.DataFrame(
            {
                "weak": rng.normal(0.0, 1.0, n),
                "strong": target.to_numpy() * 3.0 + rng.normal(0.0, 0.002, n),
            },
            index=index,
        )
        return signals, target

    def test_monthly_windows_produce_real_correlations_and_a_real_rank(self) -> None:
        signals, target = self._monthly_frame()

        rows = eigen_module.rank_signals(
            signals,
            target,
            windows={"1y": 12, "6m": 6, "3m": 3},
            forward_days=1,
            min_observations=3,
        )
        by_name = {row["signal"]: row for row in rows}

        assert by_name["strong"]["corr_1y"] is not None
        assert by_name["strong"]["n_1y"] == 12
        # Ranked on the correlation, not on the column order the frame was built in.
        assert by_name["strong"]["rank"] == 1
        assert by_name["strong"]["ranked_by"] == "corr_1y"

    def test_an_unrankable_table_says_so_instead_of_numbering_the_input_order(self) -> None:
        index = pd.date_range("2022-01-31", periods=6, freq="ME")
        signals = pd.DataFrame(
            {"a": np.ones(6), "b": np.ones(6)}, index=index
        )  # constant: no correlation is computable
        target = pd.Series(np.linspace(0.0, 0.05, 6), index=index)

        rows = eigen_module.rank_signals(
            signals, target, windows={"1y": 6}, forward_days=1, min_observations=3
        )

        assert all(row["rank"] is None for row in rows)
        assert all(row["ranked_by"] is None for row in rows)

    def test_the_text_export_omits_the_rank_column_when_nothing_is_ranked(self) -> None:
        packet = {
            "eigen": {
                "pca": {"explained_variance_ratio": [0.6, 0.4]},
                "signal_ranking": [
                    {"signal": "SPY", "rank": None, "ranked_by": None},
                    {"signal": "QQQ", "rank": None, "ranked_by": None},
                ],
                "load_bearing": [],
            }
        }

        lines = "\n".join(export_module._render_eigen(packet))

        assert "unrankable" in lines
        assert "rank" not in lines.split("Signal ranking")[1].split("\n")[1]


# ---------------------------------------------------------------------------
# Finding 7 — the spectral consistency reference must be walk-forward.
# ---------------------------------------------------------------------------


def test_spectral_consistency_reference_is_measured_the_same_way_as_the_recent_error() -> None:
    closes = _trending_closes(1400, seed=13)

    section = spectral_module.build_spectral(closes, holdout=60)
    consistency = section["consistency"]

    assert consistency["reason"] is None
    assert consistency["reference_method"] == "walk_forward_refit_and_extrapolate"
    # An out-of-sample forecast error compared against in-sample residual blocks
    # produced a wildly inflated z; against genuine forecast errors on the same
    # series the recent window is typical.
    assert abs(consistency["z"]) < 4.0
    assert consistency["historical_fit_error"] > 0.0


# ---------------------------------------------------------------------------
# Finding 8 — the entropy backtest may not trade the signal day's own move.
# ---------------------------------------------------------------------------


def test_entropy_backtest_enters_at_the_close_of_the_signal_day() -> None:
    rng = np.random.default_rng(17)
    n = 800
    returns = pd.Series(rng.normal(0.0005, 0.02, n), index=_bdays(n))

    result = entropy_module.entropy_backtest(returns, window=63, horizon_days=21)

    assert result["reason"] is None
    assert "excludes the signal day" in result["entry"]
    # The grid is now fixed-width; the remaining in-sample choice (the scale) is
    # still stated rather than hidden.
    assert result["bin_grid"] == "fixed_width_3sigma"
    assert "in-sample" in result["bin_grid_note"]

    # Reproduce the reported win rates with an explicit entry at index+1; the old
    # code used cumulative[i+h] - cumulative[i], which counts day i itself.
    series = entropy_module.entropy_series(returns, window=63)
    cumulative = np.concatenate([[0.0], np.cumsum(np.log1p(returns.to_numpy()))])
    positions = {stamp: i for i, stamp in enumerate(returns.index)}
    entropies, forwards = [], []
    for stamp, value in series.items():
        i = positions[stamp]
        if i + 1 + 21 >= cumulative.shape[0]:
            break
        entropies.append(float(value))
        forwards.append(math.expm1(cumulative[i + 1 + 21] - cumulative[i + 1]))
    entropy_array = np.asarray(entropies)
    forward_array = np.asarray(forwards)
    low = entropy_array <= float(np.quantile(entropy_array, 1.0 / 3.0))

    if result["split"] == "tercile":
        assert result["low_entropy_win_rate"] == pytest.approx(
            float(np.mean(forward_array[low] > 0.0))
        )


# ---------------------------------------------------------------------------
# Finding 9 — the gauge-fixed table must be one reference frame throughout.
# ---------------------------------------------------------------------------


def test_gauge_fixed_rolling_correlation_lives_in_the_gauge_fixed_frame() -> None:
    rng = np.random.default_rng(23)
    n = 500
    index = _bdays(n)
    market = rng.normal(0.0005, 0.011, n)
    # TLT-like: positively correlated with the ticker only through the market.
    frame = pd.DataFrame(
        {
            "NVDA": 100.0 * np.exp(np.cumsum(market * 1.8 + rng.normal(0, 0.012, n))),
            "SPY": 100.0 * np.exp(np.cumsum(market)),
            "TLT": 100.0 * np.exp(np.cumsum(market * 0.9 + rng.normal(0, 0.004, n))),
        },
        index=index,
    )

    raw = relational_module.correlation_table(
        "NVDA", frame, windows=("3m",), reference="SPY", gauge_fixed=False
    )
    fixed = relational_module.correlation_table(
        "NVDA", frame, windows=("3m",), reference="SPY", gauge_fixed=True
    )

    assert raw["TLT"]["frame"] == "raw"
    assert fixed["TLT"]["frame"] == "excess_over_SPY_zscored"
    # The two tables must no longer share a rolling value while their window
    # columns disagree: the gauge-fixed rolling leg is computed in its own frame.
    assert fixed["TLT"]["current_rolling_63d"] != pytest.approx(
        raw["TLT"]["current_rolling_63d"]
    )
    # Same 63 sessions, same frame: the rolling reading and the 3m window agree
    # in sign rather than pointing opposite ways.
    assert np.sign(fixed["TLT"]["current_rolling_63d"]) == np.sign(fixed["TLT"]["3m"])


# ---------------------------------------------------------------------------
# Finding 10 — the mixture must report how much its components actually differ.
# ---------------------------------------------------------------------------


def test_component_agreement_flags_components_that_restate_the_trailing_mean() -> None:
    horizons = {"12m": 252}
    trailing_daily = 0.0024
    trailing_12m = math.expm1(math.log1p(trailing_daily) * 252)
    components = {
        "seasonality": {"available": True, "expected_return": {"12m": trailing_12m * 1.02}},
        "regime": {"available": True, "expected_return": {"12m": trailing_12m * 0.98}},
        "factors": {"available": True, "expected_return": {"12m": trailing_12m * 1.05}},
        "macro": {"available": True, "expected_return": {"12m": 0.01}},
    }

    report = scenarios_module.component_agreement(
        components, trailing_mean_daily=trailing_daily, horizons=horizons
    )
    entry = report["horizons"]["12m"]

    assert entry["collinear"] is True
    assert set(entry["within_25pct_of_trailing_mean"]) == {
        "seasonality",
        "regime",
        "factors",
    }
    assert entry["stdev_of_means"] is not None


# ---------------------------------------------------------------------------
# Finding 11 — a 1e-5 MSE must never be printed under a "volatility" header.
# ---------------------------------------------------------------------------


def test_regime_table_prints_annualised_volatility_not_the_raw_feature() -> None:
    packet = {
        "regimes": {
            "trained_on": "SPY",
            "n_states": 3,
            "train_window_days": 2707,
            "features": ["daily_return", "vol_10d_mse"],
            "states": [
                {
                    "id": 0,
                    "label": "bull",
                    "mean_daily_return": 0.0009,
                    "volatility": 7.49e-05,
                    "vol_feature_mean": 7.49e-05,
                    "volatility_annualized": 0.0973,
                    "occupancy": 0.6,
                    "avg_duration_days": 30.0,
                }
            ],
            "current": {"label": "bull"},
            "transition_matrix": [],
        }
    }

    lines = "\n".join(export_module._render_regimes(packet))
    header = next(line for line in lines.splitlines() if "vol (ann)" in line)

    assert "vol (ann)" in header
    assert "+9.73%" in lines
    # The raw feature keeps its own explicitly named column.
    assert "vol_10d_mse feature" in header


# ---------------------------------------------------------------------------
# Finding 12 — one realized volatility, not two.
# ---------------------------------------------------------------------------


def test_scenarios_use_the_published_volatility_when_it_is_supplied() -> None:
    closes = _trending_closes(700, seed=29)

    supplied = scenarios_module.build_scenarios(close=closes, realized_vol_annual=0.42)
    fallback = scenarios_module.build_scenarios(close=closes)

    assert supplied["realized_vol_annual"] == pytest.approx(0.42)
    assert supplied["realized_vol_source"] == "caller_supplied"
    # The fallback is log returns, matching `volatility.py`, not pct_change.
    from app.prism.volatility import realized_volatility

    published = realized_volatility(closes, windows={"1y": 252})["1y"]["annualized"]
    assert fallback["realized_vol_annual"] == pytest.approx(published, rel=1e-9)


# ---------------------------------------------------------------------------
# Findings 19 & 20 — the briefing and the citation catalogue.
# ---------------------------------------------------------------------------


def _bulky_packet() -> dict[str, Any]:
    from app.prism.contract import empty_packet

    packet = empty_packet("NVDA", as_of="2026-09-01")
    packet["profile"] = {"name": "NVIDIA", "sector": "Technology", "industry": "Semis"}
    packet["profile_error"] = None
    packet["filings"] = {
        "ten_k": [
            {
                "form": "10-K",
                "filing_date": "2026-02-25",
                "report_date": "2026-01-26",
                "url": "https://sec.gov/x.htm",
                "summary": "S" * 1200,
            }
            for _ in range(2)
        ],
        "ten_q": [
            {
                "form": "10-Q",
                "filing_date": "2026-05-20",
                "report_date": "2026-04-27",
                "url": "https://sec.gov/y.htm",
                "summary": "Q" * 1200,
            }
            for _ in range(3)
        ],
        "synthesis": dict.fromkeys(("performance", "risks", "growth_opportunities"), "Z" * 1000),
    }
    packet["filings_error"] = None
    packet["news"] = {
        "items": [
            {
                "category": "company",
                "title": f"Headline {i}",
                "source": "Reuters",
                "published": "2026-08-30",
                "url": f"https://example.com/{i}",
                "summary": "N" * 320,
            }
            for i in range(18)
        ]
    }
    packet["news_error"] = None
    packet["scenarios"] = {
        "probability_horizon": "3m",
        "weights": {"seasonality": 0.4, "regime": 0.6},
        "components": {},
        "cases": {
            "bull": {"probability": 0.5, "narrative": "b", "horizons": {}},
            "bear": {"probability": 0.2, "narrative": "b", "horizons": {}},
            "neutral": {"probability": 0.3, "narrative": "n", "horizons": {}},
        },
        "entry": {"fair_value": 289.45, "current_price": 217.5},
        "timing": {"this_month": "good", "reason": "seasonal"},
    }
    packet["scenarios_error"] = None
    packet["seasonality"] = {
        "month_label": "September",
        "ticker": {"this_month": {"10y": {"mean": 0.03, "n": 10, "hit_rate": 0.7}}},
    }
    packet["seasonality_error"] = None
    packet["regimes"] = {"current": {"label": "bull", "switch_confidence": 0.7}}
    packet["regimes_error"] = None
    return packet


class TestBriefingBudget:
    def test_the_reserved_tail_always_reaches_the_model(self) -> None:
        packet = _bulky_packet()

        for cap in (25_000, 22_000, 12_000):
            briefing = memo_module.project_packet(packet, max_chars=cap)

            assert len(briefing) <= cap
            assert "## Citations" in briefing, cap
            assert "[C1]" in briefing, cap
            assert "## Scenarios" in briefing, cap
            assert "## What the engine could NOT compute" in briefing, cap

    def test_the_elastic_middle_is_what_gets_trimmed(self) -> None:
        packet = _bulky_packet()

        briefing = memo_module.project_packet(packet, max_chars=14_000)

        assert "truncated to fit the briefing budget" in briefing


class TestCitationIntegrity:
    def test_a_model_authored_citation_list_is_stripped(self) -> None:
        text = "# NVDA\n\nBody [C1].\n\n## Citations\n- [C1] Something the model made up\n"

        stripped, block = memo_module.strip_model_citations(text)

        assert "## Citations" not in stripped
        assert block is not None
        assert memo_module.citation_glosses(block) == {"C1": "Something the model made up"}

    def test_a_renumbered_id_is_caught_rather_than_shipped(self) -> None:
        citations = [
            {"id": "C7", "claim": "Trailing P/E 27.50, EV/EBITDA 25.92", "source": "prism.f"},
            {"id": "C8", "claim": "10-K filed 2026-02-25", "source": "prism.filings"},
        ]
        glosses = {"C7": "Regimes (HMM states, current posterior)", "C8": "10-K filed 2026-02-25"}

        assert memo_module.mismatched_citation_ids(glosses, citations) == ["C7"]

    def test_build_memo_falls_back_when_the_model_renumbers_the_catalogue(self) -> None:
        from tests.test_prism_narrative import (  # noqa: PLC0415
            FakeTextGenerator,
            _packet_with_scenarios,
        )

        packet = _packet_with_scenarios()
        catalogue = memo_module.build_citations(packet)
        first = catalogue[0]["id"]
        reply = {
            "action": "buy",
            "strength": "normal",
            "conviction": 0.6,
            "one_line": "ok",
            "text": (
                f"# NVDA\n\nBody [{first}].\n\n"
                f"## Citations\n- [{first}] Something entirely unrelated to bananas\n"
            ),
        }

        memo = memo_module.build_memo(
            packet, text_generator=FakeTextGenerator(json.dumps(reply))
        )

        assert memo["method"] == "deterministic"
        assert "citation ids did not resolve" in memo["reason"]

    def test_the_engine_appends_its_own_catalogue_before_the_disclaimer(self) -> None:
        from tests.test_prism_narrative import (  # noqa: PLC0415
            FakeTextGenerator,
            _packet_with_scenarios,
        )

        packet = _packet_with_scenarios()
        reply = {
            "action": "buy",
            "strength": "normal",
            "conviction": 0.6,
            "one_line": "ok",
            "text": "# NVDA\n\nBody [C1].",
        }

        memo = memo_module.build_memo(
            packet, text_generator=FakeTextGenerator(json.dumps(reply))
        )

        assert memo["method"] == "model"
        assert "## Citations" in memo["text"]
        assert memo["text"].rstrip().endswith(memo_module.DISCLAIMER)
        for citation in memo["citations"]:
            assert f"[{citation['id']}] {citation['claim']}" in memo["text"]


# ---------------------------------------------------------------------------
# Finding 17 — the model's arrays must match the published contract shape.
# ---------------------------------------------------------------------------


class TestModelArraysAreCoerced:
    def test_bare_strings_are_dropped_in_favour_of_the_engine_fallback(self) -> None:
        fallback_targets = [{"horizon": "6m", "price": 260.0, "probability": 0.5}]
        fallback_determinants = [{"name": "regime", "explanation": "bull", "weight": 0.6}]

        assert (
            memo_module.clean_exit_targets(["3m: 313"], fallback_targets) == fallback_targets
        )
        assert (
            memo_module.clean_key_determinants(
                ["AI capex cycle", "gross margin"], fallback_determinants
            )
            == fallback_determinants
        )

    def test_well_shaped_model_rows_survive(self) -> None:
        rows = memo_module.clean_exit_targets(
            [{"horizon": "3m", "price": "313.5", "probability": 0.4}, {"price": 1.0}], []
        )

        assert rows == [{"horizon": "3m", "price": 313.5, "probability": 0.4}]

    def test_build_memo_emits_contract_shaped_arrays_for_a_sloppy_reply(self) -> None:
        from tests.test_prism_narrative import (  # noqa: PLC0415
            FakeTextGenerator,
            _packet_with_scenarios,
        )

        reply = {
            "action": "buy",
            "strength": "normal",
            "conviction": 0.6,
            "one_line": "ok",
            "key_determinants": ["a"],
            "exit_targets": ["b"],
            "priced_in": ["The AI capex cycle."],
            "text": "# NVDA\n\nBody.",
        }

        memo = memo_module.build_memo(
            _packet_with_scenarios(), text_generator=FakeTextGenerator(json.dumps(reply))
        )

        for row in memo["exit_targets"]:
            assert isinstance(row, dict) and isinstance(row["horizon"], str)
        for row in memo["key_determinants"]:
            assert isinstance(row, dict)
            assert isinstance(row["name"], str) and isinstance(row["explanation"], str)
        assert all(isinstance(item, str) for item in memo["priced_in"])


# ---------------------------------------------------------------------------
# Findings 21-23 — the PDF export.
# ---------------------------------------------------------------------------


def _pdf_text(body: bytes) -> str:
    """Every decompressible content stream in the PDF, concatenated.

    ReportLab writes page content as ASCII85-then-Flate, so both layers have to
    come off before the drawn strings are visible.
    """
    out: list[str] = []
    for chunk in body.split(b"\nstream")[1:]:
        payload = chunk.split(b"endstream")[0].strip(b"\r\n")
        raw: bytes | None = None
        if payload.rstrip().endswith(b"~>"):
            try:
                raw = base64.a85decode(payload.split(b"~>")[0], adobe=False)
            except ValueError:
                raw = None
        for candidate in (raw, payload):
            if candidate is None:
                continue
            try:
                out.append(zlib.decompress(candidate).decode("latin-1"))
                break
            except zlib.error:
                continue
        else:
            out.append(payload.decode("latin-1", errors="ignore"))
    return "\n".join(out)


class TestPdfExport:
    def _packet(self) -> dict[str, Any]:
        from tests.test_prism_narrative import _packet_with_scenarios  # noqa: PLC0415

        packet = _packet_with_scenarios()
        packet["levels"] = {"torque": {"total_score": 61.0, "stage_label": "No Setup"}}
        packet["levels_error"] = None
        packet["memo"] = {
            "recommendation": {"action": "buy", "strength": "normal", "conviction": 0.4},
            "text": (
                "# NVDA - Prism memo\n\n"
                "## Thesis\n\nNVDA is a large-cap semiconductor company.\n\n"
                "## Recommendation\n\nAction buy, strength normal.\n\n"
                "## What the numbers say\n\nThe factor fit explains 60% of variance.\n"
            ),
            "exit_targets": [{"horizon": "6m", "price": 320.0, "probability": 0.4}],
            "stop_or_reassess": 184.32,
            "fair_value": 289.45,
            "citations": [],
        }
        packet["memo_error"] = None
        return packet

    def test_memo_sections_split_on_headings_with_an_executive_read(self) -> None:
        sections = export_module._memo_sections(self._packet()["memo"]["text"])

        assert list(sections)[0] == "Executive Read"
        assert "NVDA is a large-cap" in sections["Executive Read"]
        assert "Action buy" in sections["Executive Read"]
        assert "What the numbers say" in sections

    def test_no_raw_markdown_or_reportlab_markup_is_drawn(self) -> None:
        body = export_module.to_pdf(self._packet())

        drawn = _pdf_text(body)
        assert "## Thesis" not in drawn
        assert "## Recommendation" not in drawn
        # `_md_inline` used to XML-escape markup the caller deliberately built,
        # so `<font color="#b28cff">` was drawn as visible text.
        assert "font color" not in drawn

    def test_branding_and_the_reassess_label_are_prism_s(self) -> None:
        body = export_module.to_pdf(self._packet())

        drawn = _pdf_text(body)
        assert "PRISM MEMO" in drawn
        assert "Vision Memo" not in drawn
        # 184.32 is the reassess level, not a price target.
        assert "REASSESS BELOW" in drawn
        assert "TARGET LOW" not in drawn


# ---------------------------------------------------------------------------
# Finding 15 — the packet upsert must be able to infer its unique index.
# ---------------------------------------------------------------------------


def test_migration_uses_a_plain_column_unique_index() -> None:
    sql = Path("supabase/migrations/20260901120000_create_prism_tables.sql").read_text()

    statements = [
        " ".join(
            line for line in block.splitlines() if not line.strip().startswith("--")
        )
        for block in sql.split(";")
    ]
    packet_index = next(
        block for block in statements if "idx_prism_packets_ticker_as_of" in block
    )
    # PostgREST sends `on_conflict=ticker,as_of,user_id` as a plain column list,
    # which Postgres cannot infer from an expression index — 42P10 on every write.
    assert "coalesce" not in packet_index.lower()
    assert "(ticker, as_of, user_id)" in packet_index
    assert "nulls not distinct" in packet_index.lower()


# ---------------------------------------------------------------------------
# Findings 14, 16 & 18 — the HTTP surface.
# ---------------------------------------------------------------------------


class TestRouteHardening:
    @pytest.fixture()
    def app(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        from tests.test_prism_routes import (  # noqa: PLC0415
            FakeTextGenerator,
            _packet,
        )

        monkeypatch.setenv("PRISM_CACHE_DIR", str(tmp_path))
        store_module.reset_default_store()
        application = create_app()
        application.config["PRISM_STORE"] = store_module.PrismStore(
            base_dir=tmp_path, supabase=None
        )
        application.config["PRISM_TEXT_GENERATOR"] = FakeTextGenerator()
        application.config["ANTHROPIC_API_KEY"] = None
        application.config["PRISM_STORE"].save_packet(_packet())
        yield application
        store_module.reset_default_store()

    def test_build_rejects_a_malformed_as_of_before_it_reaches_the_engine(
        self, app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.prism import engine as engine_module  # noqa: PLC0415

        called: list[Any] = []

        def _explode(*_a: Any, **kwargs: Any) -> dict[str, Any]:
            called.append(kwargs.get("as_of"))
            return {}

        monkeypatch.setattr(engine_module, "build_prism_packet", _explode)

        response = app.test_client().post(
            "/api/prism", json={"ticker": "NVDA", "as_of": '9999-99-99'}
        )

        assert response.status_code == 400
        assert "ISO date" in response.get_json()["error"]
        assert called == []  # the engine was never reached

    @pytest.mark.parametrize("route", ["/api/prism/NVDA", "/api/prism/NVDA/export"])
    def test_read_routes_reject_a_malformed_as_of(self, app: Any, route: str) -> None:
        response = app.test_client().get(f"{route}?as_of=..%2F..%2Fetc")

        assert response.status_code == 400

    def test_a_spoofed_loopback_forwarded_for_no_longer_skips_admission(
        self, app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.prism import engine as engine_module  # noqa: PLC0415
        from app.prism import routes as routes_module  # noqa: PLC0415
        from tests.test_prism_routes import _packet  # noqa: PLC0415

        monkeypatch.setattr(engine_module, "build_prism_packet", lambda *_a, **_k: _packet())
        # `client_key()` now trusts only the rightmost entry, which the proxy in
        # front of this process appends. A loopback value no longer means "no
        # client", so `X-Forwarded-For: 127.0.0.1` cannot skip admission.
        held = "127.0.0.1"
        assert routes_module.try_acquire_client(held)
        try:
            response = app.test_client().post(
                "/api/prism",
                json={"ticker": "NVDA"},
                headers={"X-Forwarded-For": "127.0.0.1"},
            )
        finally:
            routes_module.release_client(held)

        assert response.status_code == 429

    def test_only_the_rightmost_forwarded_entry_is_trusted(self, app: Any) -> None:
        from app.prism import routes as routes_module  # noqa: PLC0415

        with app.test_request_context(
            "/api/prism", headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.9"}
        ):
            assert routes_module.client_key() == "203.0.113.9"

    def test_chat_rejects_an_oversized_history_turn(self, app: Any) -> None:
        response = app.test_client().post(
            "/api/prism/chat",
            json={
                "ticker": "NVDA",
                "message": "hi",
                "history": [{"role": "user", "content": "x" * 5000}],
            },
        )

        assert response.status_code == 400
        assert "history turn" in response.get_json()["error"]

    def test_chat_still_answers_a_well_formed_request(self, app: Any) -> None:
        response = app.test_client().post(
            "/api/prism/chat",
            json={
                "ticker": "NVDA",
                "message": "What regime are we in?",
                "history": [{"role": "user", "content": "earlier"}],
            },
        )

        assert response.status_code == 200
        assert response.get_json()["ticker"] == "NVDA"

    def test_an_oversized_body_is_refused(self, app: Any) -> None:
        response = app.test_client().post(
            "/api/prism",
            data=b"{}" + b" " * (routes_module_max_body() + 10),
            content_type="application/json",
        )

        assert response.status_code == 400


def routes_module_max_body() -> int:
    from app.prism.routes import MAX_BODY_BYTES  # noqa: PLC0415

    return int(MAX_BODY_BYTES)


# ---------------------------------------------------------------------------
# Calibration finding 1 — scenario expected returns were trend extrapolations.
#
# The live NVDA packet reported cases.neutral 12m = +0.68 and bull 12m = +1.79,
# with a 6m "fair value" 28% above spot, because the regime component compounded
# NVDA's own bull-regime daily mean (+0.28%/day) uncapped and the spectral
# projection extrapolated a ten-year log-price trend plus an 839-day cycle. The
# tests below pin the three mechanisms that stop it.
# ---------------------------------------------------------------------------


def _explosive_component(name: str, annual: float) -> dict[str, Any]:
    """A component that simply compounds a large daily drift forward."""
    daily = math.log1p(annual) / 252.0
    return {
        "component": name,
        "available": True,
        "reason": None,
        "confidence": 1.0,
        "basis": "test",
        "expected_return": {
            label: math.expm1(daily * days)
            for label, days in scenarios_module.HORIZONS.items()
        },
        "sigma": {
            label: 0.30 * math.sqrt(days / 252.0)
            for label, days in scenarios_module.HORIZONS.items()
        },
    }


class TestShrinkageTowardTheMarketPrior:
    def test_the_prior_is_the_loaded_market_series_compounded(self) -> None:
        market = _trending_closes(2520, seed=31)
        prior = scenarios_module.market_prior(market)

        steps = np.diff(np.log(market.to_numpy()))
        expected_daily = float(np.mean(steps))
        assert prior["source"] == "SPY_mean_daily_log_return"
        assert prior["daily_log_drift"] == pytest.approx(expected_daily)
        assert prior["by_horizon"]["12m"] == pytest.approx(math.expm1(expected_daily * 252))
        assert prior["by_horizon"]["1m"] < prior["by_horizon"]["12m"]

    def test_a_missing_market_series_is_flagged_as_an_assumption(self) -> None:
        prior = scenarios_module.market_prior(None)
        assert prior["source"] == "assumed_default"
        assert prior["annualized_drift"] == pytest.approx(
            scenarios_module.DEFAULT_MARKET_DRIFT_ANNUAL
        )
        assert "not a measurement" in prior["note"]

    def test_a_100_percent_a_year_component_is_pulled_back_toward_the_prior(self) -> None:
        market = _trending_closes(2520, seed=32)
        close = _trending_closes(2520, seed=33)
        prior = scenarios_module.market_prior(market)
        bounds = scenarios_module.empirical_return_bounds(close)

        shrunk = scenarios_module.shrink_components(
            {"regime": _explosive_component("regime", 1.0)},
            prior=prior,
            bounds=bounds,
            n_observations=int(close.shape[0]),
        )
        block = shrunk["regime"]
        raw = block["shrinkage"]["raw_expected_return"]["12m"]
        calibrated = block["expected_return"]["12m"]

        assert raw == pytest.approx(1.0, rel=1e-6)
        assert calibrated < raw
        # It must land between the prior and the raw claim, never outside them.
        assert prior["by_horizon"]["12m"] <= calibrated <= raw
        assert block["shrinkage"]["shrink_weight"]["12m"] == pytest.approx(
            1.0 - block["shrinkage"]["confidence"]["12m"]
        )
        assert calibrated == pytest.approx(
            block["shrinkage"]["confidence"]["12m"] * raw
            + block["shrinkage"]["shrink_weight"]["12m"] * prior["by_horizon"]["12m"]
        )

    def test_shrinkage_is_stronger_at_longer_horizons(self) -> None:
        market = _trending_closes(2520, seed=34)
        close = _trending_closes(2520, seed=35)
        shrunk = scenarios_module.shrink_components(
            {"regime": _explosive_component("regime", 1.0)},
            prior=scenarios_module.market_prior(market),
            bounds=scenarios_module.empirical_return_bounds(close),
            n_observations=int(close.shape[0]),
        )
        weights = shrunk["regime"]["shrinkage"]["shrink_weight"]
        # blocks / (blocks + 10) on non-overlapping horizon blocks: the further
        # out the claim, the less evidence stands behind it.
        assert weights["1m"] < weights["3m"] < weights["12m"] < weights["18m"]

    def test_a_beaten_component_keeps_less_confidence_than_an_unscored_one(self) -> None:
        market = _trending_closes(2520, seed=36)
        close = _trending_closes(2520, seed=37)
        kwargs: dict[str, Any] = {
            "prior": scenarios_module.market_prior(market),
            "bounds": scenarios_module.empirical_return_bounds(close),
            "n_observations": int(close.shape[0]),
        }
        component = {"regime": _explosive_component("regime", 1.0)}

        unscored = scenarios_module.shrink_components(component, **kwargs)
        beaten = scenarios_module.shrink_components(
            component,
            weight_evidence={"components": {"regime": {"skill": -0.4}}},
            **kwargs,
        )
        skilled = scenarios_module.shrink_components(
            component,
            weight_evidence={"components": {"regime": {"skill": 0.2}}},
            **kwargs,
        )

        assert beaten["regime"]["shrinkage"]["skill_factor"] == pytest.approx(
            scenarios_module.SKILL_FLOOR
        )
        assert unscored["regime"]["shrinkage"]["skill_factor"] == pytest.approx(
            scenarios_module.SKILL_UNMEASURED
        )
        assert skilled["regime"]["shrinkage"]["skill_factor"] == pytest.approx(1.0)
        assert (
            beaten["regime"]["expected_return"]["12m"]
            < unscored["regime"]["expected_return"]["12m"]
            < skilled["regime"]["expected_return"]["12m"]
        )

    def test_an_unavailable_component_is_left_alone(self) -> None:
        empty = {
            "spectral": {
                "component": "spectral",
                "available": False,
                "reason": "no spectral section",
                "confidence": 0.0,
                "expected_return": dict.fromkeys(scenarios_module.HORIZONS),
                "sigma": dict.fromkeys(scenarios_module.HORIZONS),
            }
        }
        shrunk = scenarios_module.shrink_components(
            empty, prior=scenarios_module.market_prior(None)
        )
        assert shrunk["spectral"]["shrinkage"]["applied"] is False
        assert shrunk["spectral"]["expected_return"] == dict.fromkeys(scenarios_module.HORIZONS)


class TestPlausibilityClamp:
    def test_bounds_are_the_tickers_own_rolling_horizon_quantiles(self) -> None:
        close = _trending_closes(2520, seed=38)
        bounds = scenarios_module.empirical_return_bounds(close)
        values = close.to_numpy(dtype=float)
        rolling = values[252:] / values[:-252] - 1.0

        assert bounds["12m"]["low"] == pytest.approx(float(np.quantile(rolling, 0.05)))
        assert bounds["12m"]["high"] == pytest.approx(float(np.quantile(rolling, 0.95)))
        assert bounds["12m"]["n"] == rolling.size

    def test_a_forecast_beyond_anything_the_name_has_done_is_clipped(self) -> None:
        close = _trending_closes(2520, seed=39)
        bounds = scenarios_module.empirical_return_bounds(close)
        # A component that is certain of a 10x, with nothing shrinking it: only
        # the clamp can stop it.
        absurd = _explosive_component("regime", 9.0)
        shrunk = scenarios_module.shrink_components(
            {"regime": absurd},
            prior={"source": "test", "by_horizon": dict.fromkeys(scenarios_module.HORIZONS)},
            bounds=bounds,
            weight_evidence={"components": {"regime": {"skill": 1.0}}},
            n_observations=int(close.shape[0]),
        )
        block = shrunk["regime"]
        assert block["shrinkage"]["clamped"]["12m"] == "high"
        assert block["expected_return"]["12m"] == pytest.approx(bounds["12m"]["high"])
        assert block["shrinkage"]["clamp_bounds"]["12m"]["high"] == pytest.approx(
            bounds["12m"]["high"]
        )

    def test_a_short_history_reports_why_it_has_no_bounds(self) -> None:
        bounds = scenarios_module.empirical_return_bounds(_trending_closes(100, seed=40))
        assert bounds["12m"]["low"] is None
        assert "closes" in bounds["12m"]["reason"]


class TestSpectralProjectionDiscipline:
    def test_the_projection_uses_a_robust_recent_trend_not_the_ten_year_slope(self) -> None:
        """A decade of re-rating followed by a flat two years must not project up."""
        index = _bdays(2520)
        rng = np.random.default_rng(41)
        # Ten years of hard trend, then two years going nowhere.
        steps = np.concatenate(
            [
                rng.normal(0.0025, 0.02, 2016),
                rng.normal(0.0000, 0.02, 504),
            ]
        )
        closes = pd.Series(10.0 * np.exp(np.cumsum(steps)), index=index, name="close")

        section = spectral_module.build_spectral(closes)

        assert section["error"] is None
        ols = section["trend"]["slope_per_day"]
        robust = section["robust_trend"]["slope_per_day"]
        assert ols > 0.0015
        assert abs(robust) < 0.3 * ols
        assert section["robust_trend"]["lookback_days"] == 504
        assert section["robust_trend"]["shrink_to_zero"] == pytest.approx(0.5)
        assert section["robust_trend"]["slope_per_day"] == pytest.approx(
            0.5 * section["robust_trend"]["median_slope_per_day"]
        )
        # The projected trend is the robust slope, not the OLS one.
        assert section["projection"]["12m"]["trend_component"] == pytest.approx(
            math.expm1(robust * 252)
        )

    def test_the_cycle_is_damped_by_r2_and_truncated_at_a_quarter_period(self) -> None:
        closes = _trending_closes(2520, seed=42)
        section = spectral_module.build_spectral(closes)

        limit = section["cycle_extrapolation_limit_days"]
        assert limit == pytest.approx(section["dominant_period_days"] * 0.25)
        assert section["cycle_damping"] == pytest.approx(section["reconstruction_r2"])

        beyond = [
            (label, entry)
            for label, entry in section["projection"].items()
            if entry["horizon_days"] > limit
        ]
        assert beyond, "expected at least one horizon past the truncation point"
        for _label, entry in beyond:
            assert entry["cycle_truncated"] is True
            assert entry["cycle_extrapolation_days"] == pytest.approx(limit)
            # Beyond the truncation point the component must not be able to
            # carry weight: the mixture shrinks anything under 0.3 hard.
            assert entry["confidence"] < 0.3
        # The frozen cycle contribution is identical for every truncated horizon.
        assert len({round(entry["cycle_component"], 12) for _, entry in beyond}) == 1

    def test_confidence_still_decays_monotonically_across_the_truncation(self) -> None:
        closes = _trending_closes(2520, seed=43)
        section = spectral_module.build_spectral(closes)
        confidences = [
            section["projection"][label]["confidence"]
            for label in ("1m", "2m", "3m", "6m", "12m", "18m")
        ]
        assert confidences == sorted(confidences, reverse=True)
        assert len(set(confidences)) == len(confidences)


def test_scenarios_publish_the_prior_the_clamp_and_every_components_shrinkage() -> None:
    close = _trending_closes(2520, seed=44)
    market = _trending_closes(2520, seed=45)

    section = scenarios_module.build_scenarios(
        close=close,
        market_close=market,
        spectral=spectral_module.build_spectral(close),
        fundamentals={"stage": {"label": "growing"}, "growth": {"revenue_yoy": 0.4}},
        ticker="TEST",
    )

    assert section["method"] == "weighted_mixture_of_shrunk_components"
    assert section["return_space"] == "log"
    assert section["mixture_parts_space"] == "log"
    assert section["prior"]["source"] == "SPY_mean_daily_log_return"
    assert section["clamp_bounds"]["12m"]["n"] > 0
    for name, component in section["components"].items():
        shrinkage = component.get("shrinkage")
        assert shrinkage is not None, name
        if not component.get("available"):
            assert shrinkage["applied"] is False
            continue
        assert set(shrinkage) >= {
            "raw_expected_return",
            "prior",
            "shrink_weight",
            "expected_return",
            "clamp_bounds",
        }
        assert shrinkage["expected_return"] == component["expected_return"]
        for label in scenarios_module.HORIZONS:
            raw = shrinkage["raw_expected_return"].get(label)
            if raw is None:
                continue
            assert 0.0 <= shrinkage["shrink_weight"][label] <= 1.0


def test_the_calibrated_mixture_stays_inside_a_believable_band() -> None:
    """The end-to-end property the live NVDA packet violated."""
    close = _trending_closes(2520, seed=46)
    market = _trending_closes(2520, seed=47)

    section = scenarios_module.build_scenarios(
        close=close,
        market_close=market,
        spectral=spectral_module.build_spectral(close),
        ticker="TEST",
    )

    neutral = section["cases"]["neutral"]["horizons"]["12m"]["expected_return"]
    bull = section["cases"]["bull"]["horizons"]["12m"]["expected_return"]
    bear = section["cases"]["bear"]["horizons"]["12m"]["expected_return"]

    assert abs(neutral) < 0.35
    for label in scenarios_module.HORIZONS:
        cases = section["cases"]
        assert (
            cases["bull"]["horizons"][label]["expected_return"]
            > cases["neutral"]["horizons"][label]["expected_return"]
            > cases["bear"]["horizons"][label]["expected_return"]
        ), label
    assert bull > neutral > bear

    # Nothing may print a loss worse than a total loss, at any horizon.
    for label in scenarios_module.HORIZONS:
        for case in ("bear", "neutral", "bull"):
            block = section["cases"][case]["horizons"][label]
            for key in ("expected_return", "p10", "p50", "p90", "median_return"):
                assert block[key] > -1.0, f"{case} {label} {key}"
            assert 0.0 < block["price_p10"] < block["price_p50"] < block["price_p90"]

    entry = section["entry"]
    spot = section["current_price"]
    # A "fair value" is only useful if it sits in the neighbourhood of the spot.
    assert 0.6 * spot < entry["fair_value"] < 1.4 * spot
    assert 0.0 < entry["bargain_below"] < entry["fair_value"] < entry["expensive_above"]


def test_the_exit_target_table_names_the_bull_case_probability() -> None:
    """A bare "probability" column reads as "probability of reaching this price"."""
    from app.prism.contract import empty_packet

    packet = empty_packet("NVDA", as_of="2026-09-01")
    packet["memo"] = {
        "recommendation": {"action": "buy", "strength": "normal", "conviction": 0.4},
        "entry_price": 200.0,
        "fair_value": 230.0,
        "stop_or_reassess": 190.0,
        "method": "test",
        "exit_targets": [
            {
                "horizon": "6m",
                "price": 260.0,
                "probability": 0.31,
                "basis": "bull-case median price at this horizon",
            }
        ],
        "text": "body",
    }
    packet["memo_error"] = None

    text = export_module.to_text(packet)

    block = text.split("Exit targets:")[1].split("Memo")[0]
    header = block.strip().splitlines()[0]
    assert "bull case p" in header
    # The bare column head read as "probability of reaching this price".
    assert "probability" not in header
    assert "each price is the bull-case median at that horizon" in block
    assert "not of reaching the price" in block
