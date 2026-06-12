from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from app.market_data import HistoryResult
from app.reclassification import (
    PROOF_STAGE_LABELS,
    ReclassificationResult,
    STALE_OLD_NOUN_INDUSTRIES,
    score_reclassification,
)


def _make_history(prices: list[float]) -> HistoryResult:
    end = datetime.now(UTC).date()
    dates = pd.to_datetime(
        [end - timedelta(days=len(prices) - 1 - i) for i in range(len(prices))]
    )
    frame = pd.DataFrame(
        {
            "Open": prices,
            "High": prices,
            "Low": prices,
            "Close": prices,
            "Volume": [1_000_000] * len(prices),
            "Adj Close": prices,
        },
        index=dates,
    )
    return HistoryResult(ticker="TEST", data=frame, provider="test", note="synthetic")


# --- MXL-shaped synthetic input --------------------------------------------


def _mxl_inputs() -> dict[str, Any]:
    profile = {
        "industry": "Semiconductors",
        "sector": "Technology",
        "longBusinessSummary": (
            "MXL designs broadband mixed-signal semiconductors. Recently the company "
            "shipped 800G optical interconnect data center PAM4 DSP products to "
            "hyperscale customers and announced a 1.6T transceiver platform with TIA "
            "and silicon photonics integration alongside coherent optical modules."
        ),
        "marketCap": 3_500_000_000,
    }
    sec_source_pack = {
        "Status": "available",
        "Filing Sections": {
            "Business": {
                "Snippet": (
                    "We supply PAM4 DSP, TIA, and retimer products into optical "
                    "transceivers shipped to hyperscale data center customers."
                )
            },
            "MD&A": {
                "Snippet": (
                    "Revenue growth in the period was driven by 800G and 1.6T data-center "
                    "interconnect ramps."
                )
            },
        },
    }
    sec_trend = {
        "Status": "available",
        "Revenue Acceleration": {"yoy": 0.56, "accelerating": True},
        "Latest Revenue": 250_000_000.0,
        "Gross Margin": 0.59,
        "Opex Run Rate": 90_000_000.0,
        "Shares Diluted": 85_000_000.0,
        "Operating Leverage": "high",
        "Segments": {"Optical": 0.7, "Broadband": 0.3},
    }
    exa_research = {
        "Status": "available",
        "Queries": {
            "product_and_customer": [
                {
                    "title": "800G optical transceiver design win at hyperscaler",
                    "snippet": "PAM4 DSP and TIA selected for data-center interconnect ramp",
                }
            ],
            "language_mutation": [
                {
                    "title": "AI optical interconnect bill of materials shifts to PAM4",
                    "snippet": "1.6T optical transceiver demand rising with hyperscale scale-out",
                }
            ],
            "sell_side_framing": [
                {
                    "title": "Analyst raises target on optical AI interconnect exposure",
                    "snippet": "PAM4 DSP, optical, transceiver, 800G, hyperscale exposure highlighted",
                }
            ],
        },
    }
    history = _make_history([20.0] * 252 + [30.0])  # used for last price; ~30
    return {
        "ticker": "MXL",
        "profile": profile,
        "history": history,
        "sec_trend": sec_trend,
        "sec_source_pack": sec_source_pack,
        "exa_research": exa_research,
    }


def test_mxl_shaped_input_picks_ai_optics_theme() -> None:
    result = score_reclassification(**_mxl_inputs())

    assert isinstance(result, ReclassificationResult)
    assert result.old_noun == STALE_OLD_NOUN_INDUSTRIES["Semiconductors"]
    verb_lower = result.primary_new_verb.lower()
    assert ("optical" in verb_lower) or ("ai traffic" in verb_lower)
    assert result.functional_layer == "Nerves"
    assert result.proof_stage in (2, 3, 4)
    assert result.proof_stage_label == PROOF_STAGE_LABELS[result.proof_stage]
    assert result.reclassification_gap > 0.5

    assert result.target_low is not None
    assert result.target_mid is not None
    assert result.target_high is not None
    current_price = 30.0
    assert result.target_mid > current_price * 1.5
    assert result.target_high >= result.target_mid >= result.target_low

    # Hidden BOM role should be a concrete optical-stack role
    assert any(
        token in result.hidden_bom_role.lower()
        for token in ("pam4", "dsp", "tia", "transceiver", "retimer")
    )

    # Top candidate should be an optics theme with multiple hits
    assert result.new_verb_candidates
    top = result.new_verb_candidates[0]
    assert top["evidence_count"] >= 3
    assert any("optics" in theme.lower() or "ai" in theme.lower() for theme in top["themes"])

    # Structural sanity
    assert len(result.catalysts) >= 3
    assert len(result.kill_criteria) >= 3
    assert len(result.diligence_gaps) >= 3


def test_stale_industrial_with_no_keywords_defaults() -> None:
    profile = {
        "industry": "Specialty Industrial Machinery",
        "sector": "Industrials",
        "longBusinessSummary": (
            "Acme makes specialty industrial machinery for general manufacturing "
            "applications, including conveyors and palletizers."
        ),
        "marketCap": 1_200_000_000,
    }
    sec_source_pack = {
        "Status": "available",
        "Filing Sections": {
            "Business": {
                "Snippet": "We sell conveyors, palletizers, and replacement parts."
            },
        },
    }
    sec_trend = {
        "Status": "available",
        "Revenue Acceleration": {"yoy": 0.02, "accelerating": False},
        "Latest Revenue": 80_000_000.0,
        "Gross Margin": 0.30,
        "Opex Run Rate": 20_000_000.0,
        "Shares Diluted": 25_000_000.0,
        "Operating Leverage": "low",
        "Segments": {},
    }
    result = score_reclassification(
        ticker="ACME",
        profile=profile,
        history=_make_history([15.0] * 100),
        sec_trend=sec_trend,
        sec_source_pack=sec_source_pack,
        exa_research={"Status": "not configured", "Queries": {}},
    )

    assert result.old_noun == STALE_OLD_NOUN_INDUSTRIES["Specialty Industrial Machinery"]
    assert result.primary_new_verb == "modernize infrastructure"
    assert result.functional_layer == "Unclassified"
    assert result.proof_stage == 0
    assert result.proof_stage_label == PROOF_STAGE_LABELS[0]
    # No theme keyword hits → hidden BOM role uses the generic stack template
    assert "specialty supplier" in result.hidden_bom_role.lower()

    # Targets should still be derivable (numbers were supplied)
    assert result.target_mid is not None
    assert result.target_low is not None
    assert result.target_high is not None


def test_none_inputs_do_not_raise() -> None:
    result = score_reclassification(
        ticker="NIL",
        profile=None,
        history=None,
        sec_trend=None,
        sec_source_pack=None,
        exa_research=None,
        torque_result=None,
    )

    assert isinstance(result, ReclassificationResult)
    assert result.old_noun == "unspecified"
    assert result.primary_new_verb == "modernize infrastructure"
    assert result.proof_stage == 0
    assert result.target_low is None
    assert result.target_mid is None
    assert result.target_high is None
    assert result.target_basis == "insufficient data to derive scenarios"
    # Diligence gaps should still surface generic items
    assert any("transcript" in gap.lower() for gap in result.diligence_gaps)


def test_old_noun_falls_back_to_industry_then_sector() -> None:
    result_with_industry = score_reclassification(
        ticker="X",
        profile={"industry": "Bespoke Widgetry", "sector": "Industrials"},
        history=None,
        sec_trend=None,
        sec_source_pack=None,
        exa_research=None,
    )
    assert result_with_industry.old_noun == "Bespoke Widgetry"

    result_sector_only = score_reclassification(
        ticker="X",
        profile={"sector": "Industrials"},
        history=None,
        sec_trend=None,
        sec_source_pack=None,
        exa_research=None,
    )
    assert result_sector_only.old_noun == "Industrials"


def test_ai_compute_theme_matches_nvidia_shaped_input() -> None:
    """NVDA-shaped: profile mentions AI / data center / GPU / training. Should
    no longer fall back to the bland 'modernize infrastructure' default."""

    profile = {
        "industry": "Semiconductors",
        "sector": "Technology",
        "longBusinessSummary": (
            "NVIDIA Corporation provides AI accelerators, GPUs, and full data "
            "center AI compute platforms. The company supplies training and "
            "inference accelerators (Hopper, Blackwell) to hyperscaler "
            "customers, alongside CUDA software and AI factory reference "
            "designs."
        ),
        "marketCap": 4_900_000_000_000,
    }
    result = score_reclassification(
        ticker="NVDA",
        profile=profile,
        history=_make_history([100.0, 110.0, 121.0]),
        sec_trend=None,
        sec_source_pack=None,
        exa_research=None,
    )
    assert result.primary_new_verb != "modernize infrastructure"
    assert result.functional_layer != "Unclassified"
    # Verb should mention AI / compute / training / accelerator
    verb_l = result.primary_new_verb.lower()
    assert any(token in verb_l for token in ("ai", "compute", "train", "acceler"))


def test_targets_fallback_from_company_facts_when_sec_trend_partial() -> None:
    """When sec_trend has no quarterly data, _compute_targets should fall back
    to SEC Source Pack > Company Facts and still produce non-None target
    bands."""

    sec_source_pack = {
        "Company Facts": {
            "Revenue": {"val": 130_000_000_000, "fp": "FY", "fy": 2026, "form": "10-K"},
            "Operating Income": {"val": 80_000_000_000, "fp": "FY", "fy": 2026, "form": "10-K"},
            "Net Income": {"val": 60_000_000_000, "fp": "FY", "fy": 2026, "form": "10-K"},
            "Shares Outstanding": {"val": 24_500_000_000, "fp": "FY", "fy": 2026, "form": "10-K"},
            "Diluted EPS": {"val": 2.45, "fp": "FY", "fy": 2026, "form": "10-K"},
        }
    }
    profile = {
        "industry": "Semiconductors",
        "longBusinessSummary": "AI accelerator company; ships GPUs into data centers.",
        "revenueGrowth": 0.55,
        "marketCap": 4_900_000_000_000,
    }
    result = score_reclassification(
        ticker="NVDA",
        profile=profile,
        history=_make_history([200.0] * 30),
        sec_trend={"Status": "error", "Errors": ["partial"]},
        sec_source_pack=sec_source_pack,
        exa_research=None,
    )
    assert result.target_low is not None
    assert result.target_mid is not None
    assert result.target_high is not None
    assert result.target_low < result.target_mid <= result.target_high
    assert result.target_basis != "insufficient data to derive scenarios"


def test_targets_clean_up_when_data_partial() -> None:
    # Missing shares → should not be able to compute targets
    sec_trend = {
        "Status": "available",
        "Revenue Acceleration": {"yoy": 0.20, "accelerating": True},
        "Latest Revenue": 100_000_000.0,
        "Gross Margin": 0.45,
        "Opex Run Rate": 30_000_000.0,
        # No Shares Diluted
    }
    result = score_reclassification(
        ticker="PART",
        profile={"industry": "Semiconductors", "longBusinessSummary": "optical transceiver dsp pam4"},
        history=_make_history([10.0, 11.0, 12.0]),
        sec_trend=sec_trend,
        sec_source_pack=None,
        exa_research=None,
    )
    assert result.target_low is None
    assert result.target_mid is None
    assert result.target_high is None
    assert result.target_basis == "insufficient data to derive scenarios"
