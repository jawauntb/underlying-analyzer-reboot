from __future__ import annotations

import base64
from typing import Any, cast

import pytest

from app.memo_pdf import MemoPdfPayload, render_memo_pdf


# A single transparent 1x1 PNG, base64-encoded.
_ONE_PIXEL_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjC"
    "B0C8AAAAASUVORK5CYII="
)


def test_minimal_payload_returns_pdf_bytes() -> None:
    payload = MemoPdfPayload(
        ticker="ACME",
        company_name="Acme Corp",
        memo_text="# Vision Memo\n\nAcme is doing things.",
        recommendation="Buy",
    )
    out = render_memo_pdf(payload)
    assert isinstance(out, bytes)
    assert out.startswith(b"%PDF-")
    assert len(out) > 1500


def test_full_payload_renders_with_all_sections() -> None:
    memo = """# Vision Memo

## Executive Read

Acme is **mispriced** because its old noun (a *server rack vendor*) does not
match the new verb (an AI inference layer).

### Why now

- Multi-quarter gross margin expansion
- New product cadence inflecting
- `Inference attach-rate` rising

## Financial Bend

| Metric | FY23 | FY24E | FY25E |
| ------ | ---- | ----- | ----- |
| Revenue | 1.2B | 1.8B | 2.6B |
| Gross Margin | 32% | 38% | 44% |

## Bull case

The company emerges as the [arms dealer](https://example.com) of inference.

## Bear case

Hyperscalers in-source.
"""
    charts = [
        {
            "title": "Torque components",
            "data": _ONE_PIXEL_PNG,
            "mime": "image/png",
            "caption": "Composite torque snapshot.",
        },
        {
            "title": "Revenue vs gross margin",
            "data": _ONE_PIXEL_PNG,
            "mime": "image/png",
            "caption": "Trailing 8 quarters.",
        },
    ]
    payload = MemoPdfPayload(
        ticker="ACME",
        company_name="Acme Corporation",
        sector="Technology",
        industry="Servers & Networking",
        generated_at="2026-06-12T10:30:00Z",
        recommendation="Strong Buy",
        target_low=120.0,
        target_mid=185.0,
        target_high=240.0,
        current_price=98.50,
        market_cap=42_000_000_000,
        old_noun="Server rack vendor",
        new_verb="AI inference fabric",
        hidden_bom_role="Power & thermal substrate for HBM stacks",
        functional_layer="Layer 2: System integration",
        proof_stage=2,
        proof_stage_label="Financial Bend",
        reclassification_gap=0.62,
        torque_score=78.4,
        torque_stage="Coiled Spring",
        torque_components=[
            {"name": "Earnings revisions", "score": 82, "weight": 0.25, "detail": "+18% NTM EPS"},
            {"name": "Estimate dispersion", "score": 71, "weight": 0.15, "detail": "Widening"},
            {"name": "Insider activity", "score": 45, "weight": 0.10, "detail": "Mixed"},
            {"name": "Reclassification gap", "score": 88, "weight": 0.30, "detail": "High torque"},
            {"name": "Liquidity flow", "score": 63, "weight": 0.10, "detail": "Improving"},
            {"name": "Catalyst calendar", "score": 70, "weight": 0.10, "detail": "Q3 print"},
        ],
        memo_text=memo,
        memo_sections={
            "Executive Read": (
                "Acme has quietly transitioned from a commodity server vendor "
                "into the **default substrate** for AI inference clusters. The "
                "Street still models it as a *hardware reseller*."
            )
        },
        charts=charts,
        scenarios=[
            {
                "name": "Bear",
                "price": "$95",
                "rev_growth": "8%",
                "gm": "30%",
                "eps": "$3.20",
                "multiple": "18x",
                "notes": "Hyperscaler in-sourcing accelerates.",
            },
            {
                "name": "Base",
                "price": "$185",
                "rev_growth": "22%",
                "gm": "38%",
                "eps": "$6.50",
                "multiple": "28x",
                "notes": "Reclassification proceeds on schedule.",
            },
            {
                "name": "Bull",
                "price": "$240",
                "rev_growth": "35%",
                "gm": "44%",
                "eps": "$8.40",
                "multiple": "32x",
                "notes": "Inference attach beats expectations.",
            },
        ],
        citations=[
            {
                "label": "Acme FY24 10-K",
                "source": "SEC EDGAR",
                "url": "https://www.sec.gov/some/path",
                "filed_date": "2025-02-14",
            },
            {
                "label": "Earnings call transcript",
                "source": "Acme IR",
                "url": "https://ir.acme.com/q4-2025",
                "filed_date": "2026-02-04",
            },
        ],
        catalysts=[
            "Q3 inference attach disclosure",
            "Hyperscaler renewal cycle",
            "New HBM partner announcement",
        ],
        kill_criteria=[
            "Gross margin reversion below 30%",
            "Loss of top-3 hyperscaler customer",
            "Reclassification gap collapses",
        ],
        diligence_gaps=[
            "Customer concentration disclosure",
            "Long-term inference TAM",
        ],
    )
    out = render_memo_pdf(payload)
    assert out.startswith(b"%PDF-")
    assert len(out) > 5000


def test_charts_with_one_pixel_png_render() -> None:
    payload = MemoPdfPayload(
        ticker="TEST",
        company_name="Test Co",
        memo_text="Body.",
        recommendation="Hold",
        charts=[
            {"title": "Tiny", "data": _ONE_PIXEL_PNG, "mime": "image/png"},
        ],
    )
    out = render_memo_pdf(payload)
    assert out.startswith(b"%PDF-")
    assert len(out) > 1500


def test_malformed_inputs_do_not_raise() -> None:
    # None for required-typed fields.
    payload = MemoPdfPayload(
        ticker=cast(str, None),
        company_name=cast(str, None),
        memo_text=cast(str, None),
        recommendation=cast(str, None),
    )
    out = render_memo_pdf(payload)
    assert isinstance(out, bytes)
    assert out.startswith(b"%PDF-")


def test_non_payload_argument_returns_error_pdf() -> None:
    out = render_memo_pdf(cast(Any, "not a payload"))
    assert isinstance(out, bytes)
    assert out.startswith(b"%PDF-")


def test_garbage_chart_data_is_skipped_gracefully() -> None:
    payload = MemoPdfPayload(
        ticker="BAD",
        company_name="Bad Co",
        memo_text="Hello.",
        recommendation="Sell",
        charts=[
            {"title": "Broken", "data": "not-real-base64!!!", "mime": "image/png"},
            {"title": "Empty", "data": "", "mime": "image/png"},
            {"title": "Missing"},
            "not a dict",  # type: ignore[list-item]
        ],
    )
    out = render_memo_pdf(payload)
    assert out.startswith(b"%PDF-")


def test_markdown_table_renders() -> None:
    body = """## Numbers

| Period | Revenue | Margin |
| ------ | ------- | ------ |
| Q1 | $1.0B | 32% |
| Q2 | $1.1B | 34% |
| Q3 | $1.3B | 37% |
"""
    payload = MemoPdfPayload(
        ticker="TBL",
        company_name="Table Co",
        memo_text=body,
        recommendation="Buy",
    )
    out = render_memo_pdf(payload)
    assert out.startswith(b"%PDF-")
    # Reasonable size — table + headers should add bulk.
    assert len(out) > 2500


@pytest.mark.parametrize(
    "rec",
    ["Strong Buy", "Buy", "Hold", "Neutral", "Sell", "Strong Sell", "Unknown"],
)
def test_recommendation_variants_all_render(rec: str) -> None:
    payload = MemoPdfPayload(
        ticker="REC",
        company_name="Rec Co",
        memo_text="Body.",
        recommendation=rec,
    )
    out = render_memo_pdf(payload)
    assert out.startswith(b"%PDF-")
