"""Tests for the Vision v2 orchestrator and prompt."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pandas as pd
import pytest

from app.market_data import HistoryResult
from app.vision_v2 import (
    DEFAULT_VISION_V2_MAX_TOKENS,
    DEFAULT_VISION_V2_TEMPERATURE,
    VISION_V2_SECTIONS,
    VISION_V2_SYSTEM,
    build_vision_v2_data,
    build_vision_v2_memo,
    parse_memo_sections,
    stream_vision_v2_text,
    vision_v2_prompt,
    vision_v2_system_prompt,
)


# ---------------------------------------------------------------------------
# Fake collaborators
# ---------------------------------------------------------------------------


class FakeMarketDataClient:
    """Minimal MarketDataClient stub returning enough data for
    ``build_stock_fax_data`` to assemble a report without network calls."""

    def get_history(self, ticker: str, *, period: str = "2y", **_: Any) -> HistoryResult:
        index = pd.date_range("2024-01-01", periods=260, freq="B")
        prices = pd.Series(
            [100 + (i * 0.1) for i in range(len(index))],
            index=index,
            name="Adj Close",
        )
        data = pd.DataFrame(
            {
                "Open": prices,
                "High": prices * 1.01,
                "Low": prices * 0.99,
                "Close": prices,
                "Adj Close": prices,
                "Volume": [1_000_000] * len(index),
            }
        )
        return HistoryResult(ticker=ticker, data=data, provider="fake", note="synthetic")

    def get_profile(self, ticker: str) -> dict[str, Any]:
        return {
            "longName": f"{ticker} Holdings",
            "shortName": ticker,
            "sector": "Industrials",
            "industry": "Specialty Components",
            "longBusinessSummary": "A specialty parts maker quietly pivoting to AI infra.",
            "country": "United States",
            "website": "https://example.com",
            "fullTimeEmployees": 4200,
            "marketCap": 5_000_000_000,
            "trailingPE": 22.0,
            "forwardPE": 18.0,
            "beta": 1.15,
            "priceToSalesTrailing12Months": 3.4,
            "priceToBook": 4.2,
            "revenueGrowth": 0.18,
            "profitMargins": 0.11,
            "returnOnEquity": 0.15,
            "debtToEquity": 0.62,
            "totalRevenue": 1_200_000_000,
            "recommendationKey": "buy",
            "targetMeanPrice": 145.0,
            "numberOfAnalystOpinions": 12,
        }


class FakeGeneratedText:
    text = (
        "# MXL Vision v2 — Reclassification Memo\n\n"
        "### 1. Executive Read\n\nThesis body.\n\n"
        "### 13. Final Rating + Target Price Band\n\n"
        "Rating: Buy. Target band: $10 – $20 – $30."
    )
    provider = "anthropic"
    model = "claude-test"


class FakeStreamingGenerator:
    """Generator that records the system + prompt and returns canned chunks."""

    def __init__(self) -> None:
        self.system: str | None = None
        self.prompt: str | None = None
        self.max_tokens: int | None = None
        self.temperature: float | None = None

    def generate_text(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 700,
        temperature: float = 0.2,
    ) -> FakeGeneratedText:
        self.system = system
        self.prompt = prompt
        self.max_tokens = max_tokens
        self.temperature = temperature
        return FakeGeneratedText()

    def stream_text(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 700,
        temperature: float = 0.2,
    ):
        self.system = system
        self.prompt = prompt
        self.max_tokens = max_tokens
        self.temperature = temperature
        for chunk in ("Hello ", "Vision ", "v2."):
            yield chunk


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def test_vision_v2_system_prompt_returns_non_empty_string() -> None:
    system = vision_v2_system_prompt()

    assert isinstance(system, str)
    assert system.strip()
    assert system == VISION_V2_SYSTEM


def test_vision_v2_system_prompt_contains_key_framework_phrases() -> None:
    system = vision_v2_system_prompt().lower()

    assert "old noun" in system
    assert "new verb" in system
    assert "torque" in system
    assert "reclassification" in system
    assert "proof ladder" in system or "proof-ladder" in system
    assert "kill criteria" in system


# ---------------------------------------------------------------------------
# User prompt
# ---------------------------------------------------------------------------


def test_vision_v2_prompt_lists_all_thirteen_sections() -> None:
    prompt = vision_v2_prompt({"Ticker": "MXL"})

    assert isinstance(prompt, str)
    assert "MXL" in prompt
    for section in VISION_V2_SECTIONS:
        assert section in prompt, f"Missing section in prompt: {section}"


def test_vision_v2_prompt_embeds_supplied_data_as_json() -> None:
    prompt = vision_v2_prompt({"Ticker": "MXL", "Name": "MaxLinear Inc"})

    assert "```json" in prompt
    assert "MaxLinear Inc" in prompt


def test_vision_v2_prompt_omits_export_rows_from_payload() -> None:
    big_rows = [{"date": "2024-01-01", "close": 1.0}] * 5
    prompt = vision_v2_prompt({"Ticker": "MXL", "Export Rows": big_rows})

    # The literal Export Rows key should not appear inside the JSON payload
    # block embedded in the prompt.
    assert '"Export Rows"' not in prompt


# ---------------------------------------------------------------------------
# build_vision_v2_data
# ---------------------------------------------------------------------------


def test_build_vision_v2_data_returns_expected_keys_with_no_optionals() -> None:
    client = FakeMarketDataClient()

    report = build_vision_v2_data(client, "mxl", sec_client=None, exa_client=None)

    expected_keys = {
        "Ticker",
        "Name",
        "Sector",
        "Industry",
        "Profile",
        "Snapshot",
        "History Summary",
        "SEC Source Pack",
        "SEC Trend Pack",
        "Earnings Source Pack",
        "Exa Research Pack",
        "Torque",
        "Reclassification",
        "Memo Inputs",
        "Export Rows",
    }
    missing = expected_keys - set(report.keys())
    assert not missing, f"Missing keys: {missing}"

    assert report["Ticker"] == "MXL"
    assert isinstance(report["Memo Inputs"], dict)
    assert "Data Availability" in report["Memo Inputs"]


def test_build_vision_v2_data_handles_missing_optional_modules() -> None:
    client = FakeMarketDataClient()

    # Even when optional helpers blow up, the orchestrator must return
    # a report rather than raising.
    with patch("app.vision_v2.build_research_pack", None), patch(
        "app.vision_v2.build_sec_trend_pack", None
    ), patch("app.vision_v2.compute_torque_score", None), patch(
        "app.vision_v2.score_reclassification", None
    ):
        report = build_vision_v2_data(client, "MXL")

    assert report["SEC Trend Pack"] is None
    assert report["Exa Research Pack"] is None
    assert report["Torque"] is None
    assert report["Reclassification"] is None


def test_build_vision_v2_data_swallows_optional_exceptions() -> None:
    client = FakeMarketDataClient()

    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("trend pack offline")

    with patch("app.vision_v2.build_sec_trend_pack", boom):
        report = build_vision_v2_data(client, "MXL")

    sec_trend = report["SEC Trend Pack"]
    assert isinstance(sec_trend, dict)
    assert sec_trend.get("Status") == "error"
    assert any("trend pack offline" in err for err in sec_trend.get("Errors", []))


# ---------------------------------------------------------------------------
# Memo Sections parser
# ---------------------------------------------------------------------------


def test_parse_memo_sections_splits_three_section_memo() -> None:
    memo = (
        "# Vision v2\n\n"
        "Some intro.\n\n"
        "### 1. Executive Read\n\n"
        "Body of the executive read.\n\n"
        "### 2. Old Noun → New Verb\n\n"
        "Old noun body.\n\n"
        "### 3. Torque Math\n\n"
        "Math body."
    )

    sections = parse_memo_sections(memo)

    assert sections["Executive Read"].startswith("Body of the executive read")
    assert sections["Old Noun → New Verb"].startswith("Old noun body")
    assert sections["Torque Math"] == "Math body."
    # Preamble preserved
    assert "Preamble" in sections
    assert "# Vision v2" in sections["Preamble"]


def test_parse_memo_sections_handles_empty_and_unheadered_input() -> None:
    assert parse_memo_sections("") == {}
    plain = parse_memo_sections("Just a blob with no headers.")
    assert plain == {"Preamble": "Just a blob with no headers."}


# ---------------------------------------------------------------------------
# build_vision_v2_memo wiring
# ---------------------------------------------------------------------------


def test_build_vision_v2_memo_passes_through_generator() -> None:
    client = FakeMarketDataClient()
    generator = FakeStreamingGenerator()

    result = build_vision_v2_memo(
        client,
        "MXL",
        text_generator=generator,
    )

    assert result["Memo Text"].startswith("# MXL Vision v2")
    assert result["Text Model"] == "claude-test"
    assert result["Text Provider"] == "anthropic"
    assert generator.system == VISION_V2_SYSTEM
    assert generator.max_tokens == DEFAULT_VISION_V2_MAX_TOKENS
    assert generator.temperature == pytest.approx(DEFAULT_VISION_V2_TEMPERATURE)
    assert "Executive Read" in result["Memo Sections"]


def test_stream_vision_v2_text_uses_stream_when_available() -> None:
    generator = FakeStreamingGenerator()
    chunks = list(
        stream_vision_v2_text(
            {"Ticker": "MXL"},
            text_generator=generator,
        )
    )

    assert chunks == ["Hello ", "Vision ", "v2."]
    assert generator.system == VISION_V2_SYSTEM
    assert generator.max_tokens == DEFAULT_VISION_V2_MAX_TOKENS
