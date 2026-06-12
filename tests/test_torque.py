"""Tests for the Misclassified Revenue Torque composite indicator."""

from __future__ import annotations

import base64
from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.market_data import HistoryResult
from app.torque import (
    TorqueComponent,
    TorqueResult,
    compute_torque_score,
    render_torque_chart,
    score_revenue_inflection,
    score_stale_valuation,
    score_technical_discipline,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _constructive_history(ticker: str = "MXLT") -> HistoryResult:
    """A 1Y price history that is positive but not extended: roughly +35%
    over the year with realistic noise so RSI lands in the 50-65 zone and
    the price sits comfortably above the 50DMA but below extension."""
    rng = np.random.default_rng(seed=42)
    index = pd.date_range(date(2025, 1, 2), periods=260, freq="B")
    # Random-walk uptrend: small positive drift + meaningful daily noise so
    # gains and losses balance enough to keep RSI in a healthy band.
    daily_returns = rng.normal(loc=0.0011, scale=0.014, size=len(index))
    close = 100.0 * np.cumprod(1.0 + daily_returns)
    # Shape so the most recent 30 days are a mild pullback, not a peak —
    # keeps price ~5-12% above the 50DMA, not blown off.
    close[-30:] = close[-30:] * np.linspace(1.0, 0.96, 30)
    frame = pd.DataFrame(
        {
            "Open": close - 0.4,
            "High": close + 0.7,
            "Low": close - 0.7,
            "Close": close,
            "Volume": np.full(len(index), 1_500_000, dtype=float),
        },
        index=index,
    )
    return HistoryResult(ticker=ticker, data=frame, provider="test", note="synthetic")


def _blowoff_history(ticker: str = "HOTZ") -> HistoryResult:
    """A vertical move: +160% in three months drives RSI > 80 and price
    well above the 50DMA — should hit the extension guard."""
    index = pd.date_range(date(2025, 1, 2), periods=260, freq="B")
    flat = np.full(len(index) - 63, 50.0)
    ramp = np.linspace(50.0, 130.0, 63)
    close = np.concatenate([flat, ramp])
    frame = pd.DataFrame(
        {
            "Open": close - 0.1,
            "High": close + 0.2,
            "Low": close - 0.2,
            "Close": close,
            "Volume": np.full(len(index), 2_000_000, dtype=float),
        },
        index=index,
    )
    return HistoryResult(ticker=ticker, data=frame, provider="test", note="synthetic")


def _mxl_sec_trend() -> dict:
    """The textbook MXL fundamental pack."""
    return {
        "Status": "available",
        "Revenue Acceleration": {
            "qoq_latest": 0.18,
            "yoy_latest": 0.56,
            "accelerating": True,
        },
        "Margin Trajectory": {
            "gross_margin_direction": "rising",
            "revenue_direction": "rising",
        },
        "Operating Leverage": {"label": "high", "value": 2.5},
        "Metrics": {
            "Revenue": {
                "yoy_growth_latest": 0.56,
                "quarterly": [
                    {"label": "Q1'24", "value": 410.0},
                    {"label": "Q2'24", "value": 430.0},
                    {"label": "Q3'24", "value": 455.0},
                    {"label": "Q4'24", "value": 480.0},
                    {"label": "Q1'25", "value": 520.0},
                    {"label": "Q2'25", "value": 565.0},
                    {"label": "Q3'25", "value": 615.0},
                    {"label": "Q4'25", "value": 690.0},
                ],
            },
            "Gross Margin": {
                "latest": 59.0,
                "quarterly": [
                    {"label": "Q1'24", "value": 52.0},
                    {"label": "Q2'24", "value": 53.5},
                    {"label": "Q3'24", "value": 54.5},
                    {"label": "Q4'24", "value": 55.8},
                    {"label": "Q1'25", "value": 56.5},
                    {"label": "Q2'25", "value": 57.4},
                    {"label": "Q3'25", "value": 58.2},
                    {"label": "Q4'25", "value": 59.0},
                ],
            },
            "Operating Margin": {
                "quarterly": [
                    {"label": "Q1'24", "value": 8.0},
                    {"label": "Q2'24", "value": 10.5},
                    {"label": "Q3'24", "value": 12.0},
                    {"label": "Q4'24", "value": 14.0},
                    {"label": "Q1'25", "value": 16.5},
                    {"label": "Q2'25", "value": 19.0},
                    {"label": "Q3'25", "value": 21.0},
                    {"label": "Q4'25", "value": 24.0},
                ],
            },
        },
    }


def _ugly_sec_trend() -> dict:
    """Declining revenue, mediocre margins, no torque."""
    return {
        "Status": "available",
        "Revenue Acceleration": {
            "qoq_latest": -0.04,
            "yoy_latest": -0.12,
            "accelerating": False,
        },
        "Margin Trajectory": {
            "gross_margin_direction": "falling",
            "revenue_direction": "falling",
        },
        "Operating Leverage": {"label": "negative", "value": -0.5},
        "Metrics": {
            "Revenue": {
                "yoy_growth_latest": -0.12,
                "quarterly": [
                    {"label": "Q1'24", "value": 300.0},
                    {"label": "Q2'24", "value": 290.0},
                    {"label": "Q3'24", "value": 285.0},
                    {"label": "Q4'24", "value": 275.0},
                    {"label": "Q1'25", "value": 265.0},
                    {"label": "Q2'25", "value": 260.0},
                    {"label": "Q3'25", "value": 255.0},
                    {"label": "Q4'25", "value": 242.0},
                ],
            },
            "Gross Margin": {"latest": 20.0},
            "Operating Margin": {
                "quarterly": [
                    {"label": "Q1'24", "value": 3.0},
                    {"label": "Q2'24", "value": 1.5},
                    {"label": "Q3'24", "value": 0.5},
                    {"label": "Q4'24", "value": -0.5},
                    {"label": "Q1'25", "value": -1.5},
                    {"label": "Q2'25", "value": -2.0},
                    {"label": "Q3'25", "value": -3.0},
                    {"label": "Q4'25", "value": -4.0},
                ],
            },
        },
    }


# ---------------------------------------------------------------------------
# Score: MXL setup
# ---------------------------------------------------------------------------


def test_mxl_setup_scores_in_coiled_spring_or_inflecting() -> None:
    history = _constructive_history()
    sec_trend = _mxl_sec_trend()
    profile = {
        "trailingPE": 28.0,
        "priceToSalesTrailing12Months": 2.2,
        "marketCap": 1_200_000_000.0,
        "industry": "Semiconductors",
    }

    result = compute_torque_score(
        history=history, sec_trend=sec_trend, profile=profile
    )

    assert isinstance(result, TorqueResult)
    assert result.total_score >= 80.0, (
        f"expected >=80, got {result.total_score}; comps={[(c.name, c.score) for c in result.components]}"
    )
    assert result.stage_label in {"Coiled Spring", "Inflecting"}
    assert result.recommendation in {"BUY", "BUY SETUP"}

    # Each component should appear with the contracted weight.
    weights = {c.name: c.weight for c in result.components}
    assert weights["Revenue Inflection"] == 0.25
    assert weights["Margin Torque"] == 0.20
    assert weights["Reclassification Lag"] == 0.10

    # Revenue inflection should be near the top of its range.
    rev = next(c for c in result.components if c.name == "Revenue Inflection")
    assert rev.score >= 90.0

    # Reclassification lag should flag the stale 'Semiconductors' tag.
    rec = next(c for c in result.components if c.name == "Reclassification Lag")
    assert rec.score == 80.0


# ---------------------------------------------------------------------------
# Score: ugly stock
# ---------------------------------------------------------------------------


def test_stale_ugly_stock_scores_low() -> None:
    history = _blowoff_history()
    sec_trend = _ugly_sec_trend()
    profile = {
        "priceToSalesTrailing12Months": 12.0,
        "marketCap": 50_000_000_000.0,
        "industry": "Software-Infrastructure",
    }

    result = compute_torque_score(
        history=history, sec_trend=sec_trend, profile=profile
    )

    assert result.total_score < 30.0, (
        f"expected <30, got {result.total_score}; comps={[(c.name, c.score) for c in result.components]}"
    )
    # Recommendation should not be BUY / BUY SETUP.
    assert result.recommendation not in {"BUY", "BUY SETUP"}


# ---------------------------------------------------------------------------
# Robustness: None inputs
# ---------------------------------------------------------------------------


def test_none_inputs_do_not_raise() -> None:
    result = compute_torque_score(history=None, sec_trend=None, profile=None)
    assert isinstance(result, TorqueResult)
    assert 0.0 <= result.total_score <= 100.0
    assert "no fundamental data" in result.stage_detail
    # All components should still be present.
    assert {c.name for c in result.components} == {
        "Revenue Inflection",
        "Margin Torque",
        "Stale Valuation",
        "Operating Leverage",
        "Technical Discipline",
        "Reclassification Lag",
    }


def test_sec_trend_unavailable_uses_neutral_fundamentals() -> None:
    sec_trend = {"Status": "unavailable"}
    history = _constructive_history()
    result = compute_torque_score(history=history, sec_trend=sec_trend, profile={})
    rev = next(c for c in result.components if c.name == "Revenue Inflection")
    margin = next(c for c in result.components if c.name == "Margin Torque")
    op_lev = next(c for c in result.components if c.name == "Operating Leverage")
    assert rev.score == 50.0
    assert margin.score == 50.0
    assert op_lev.score == 50.0
    assert "no fundamental data" in result.stage_detail


# ---------------------------------------------------------------------------
# Component-level sanity
# ---------------------------------------------------------------------------


def test_revenue_inflection_handles_decline() -> None:
    sec_trend = {
        "Status": "available",
        "Revenue Acceleration": {
            "qoq_latest": -0.05,
            "yoy_latest": -0.20,
            "accelerating": False,
        },
        "Metrics": {"Revenue": {"yoy_growth_latest": -0.20}},
    }
    comp = score_revenue_inflection(sec_trend)
    assert comp.score == 0.0


def test_revenue_inflection_handles_hyper_growth() -> None:
    sec_trend = {
        "Status": "available",
        "Revenue Acceleration": {
            "qoq_latest": 0.30,
            "yoy_latest": 0.78,
            "accelerating": True,
        },
        "Metrics": {"Revenue": {"yoy_growth_latest": 0.78}},
    }
    comp = score_revenue_inflection(sec_trend)
    assert comp.score == 100.0


def test_stale_valuation_rewards_low_ps_high_growth() -> None:
    profile = {
        "priceToSalesTrailing12Months": 2.0,
        "marketCap": 2_000_000_000.0,
    }
    sec_trend = {
        "Status": "available",
        "Metrics": {"Revenue": {"yoy_growth_latest": 0.40}},
    }
    comp = score_stale_valuation(profile, sec_trend=sec_trend, market_cap_override=None)
    # 100 - 2*15 + 10 mid-cap bonus = 80
    assert comp.score >= 70.0


def test_stale_valuation_penalizes_nosebleed_ps() -> None:
    profile = {
        "priceToSalesTrailing12Months": 15.0,
        "marketCap": 100_000_000_000.0,
    }
    sec_trend = {
        "Status": "available",
        "Metrics": {"Revenue": {"yoy_growth_latest": 0.10}},
    }
    comp = score_stale_valuation(profile, sec_trend=sec_trend, market_cap_override=None)
    assert comp.score <= 10.0


def test_technical_discipline_flags_blowoff() -> None:
    comp = score_technical_discipline(_blowoff_history())
    assert comp.score < 30.0


def test_technical_discipline_rewards_constructive() -> None:
    comp = score_technical_discipline(_constructive_history())
    assert comp.score >= 50.0


# ---------------------------------------------------------------------------
# Chart rendering
# ---------------------------------------------------------------------------


def test_render_torque_chart_returns_png() -> None:
    history = _constructive_history()
    sec_trend = _mxl_sec_trend()
    profile = {
        "priceToSalesTrailing12Months": 2.2,
        "marketCap": 1_200_000_000.0,
        "industry": "Semiconductors",
    }

    image, meta = render_torque_chart(
        history=history, sec_trend=sec_trend, profile=profile
    )

    assert image.mime == "image/png"
    assert image.filename.endswith("-torque.png")
    # Base64 payload must be non-empty and decode to a real PNG.
    decoded = base64.b64decode(image.data)
    assert len(decoded) > 1000
    assert decoded[:8] == b"\x89PNG\r\n\x1a\n"

    # Meta carries the full torque + component breakdown.
    assert meta["ticker"] == "MXLT"
    assert "total_score" in meta
    assert meta["stage_label"] in {
        "Coiled Spring",
        "Inflecting",
        "Proof Phase",
        "Renaming Phase",
        "Extended",
        "No Setup",
    }
    assert set(meta["components"].keys()) == set(
        {c.name for c in [
            TorqueComponent("Revenue Inflection", 0, 0, ""),
            TorqueComponent("Margin Torque", 0, 0, ""),
            TorqueComponent("Stale Valuation", 0, 0, ""),
            TorqueComponent("Operating Leverage", 0, 0, ""),
            TorqueComponent("Technical Discipline", 0, 0, ""),
            TorqueComponent("Reclassification Lag", 0, 0, ""),
        ]}
    )
    assert meta["fundamental_data_available"] is True


def test_render_torque_chart_with_none_history() -> None:
    # Should still render (with a NO PRICE HISTORY placeholder panel).
    image, meta = render_torque_chart(history=None, sec_trend=None, profile=None)
    decoded = base64.b64decode(image.data)
    assert decoded[:8] == b"\x89PNG\r\n\x1a\n"
    assert meta["fundamental_data_available"] is False


def test_render_torque_chart_accepts_precomputed_result() -> None:
    history = _constructive_history()
    sec_trend = _mxl_sec_trend()
    profile = {
        "priceToSalesTrailing12Months": 2.2,
        "marketCap": 1_200_000_000.0,
        "industry": "Semiconductors",
    }
    torque = compute_torque_score(
        history=history, sec_trend=sec_trend, profile=profile
    )
    image, meta = render_torque_chart(
        history=history, sec_trend=sec_trend, profile=profile, torque=torque
    )
    assert meta["total_score"] == pytest.approx(torque.total_score)
    assert meta["stage_label"] == torque.stage_label
