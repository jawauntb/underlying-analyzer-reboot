"""Vision v2 orchestrator + prompt.

This module is the upgrade to Vision v1 (``app.tools.build_market_memo``).
Where Vision v1 returns a ~950-1300 word generic "summary with rating" memo,
Vision v2 produces a structured, MXL-quality analyst memo built around the
"misclassified revenue torque" framework: an old-noun-vs-new-verb
reclassification thesis, an explicit financial bend across multiple quarters,
torque math, a scenario table with target prices, catalysts, kill criteria,
proof-ladder stage and entry discipline. Target length is ~2500-4500 words
with an 8000-token budget.

The module is intentionally defensive: the modules that supply
reclassification scoring, torque math, Exa web research, and multi-quarter
SEC trend data are being built in parallel and may not exist yet. All those
imports are wrapped in try/except ImportError and the orchestrator degrades
gracefully when a data source is missing.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

import requests

from app.anthropic import (
    AnthropicTextClient,
    GeneratedText,
    StreamingTextGenerator,
    TextGenerator,
)

# ---------------------------------------------------------------------------
# Defensive optional imports. Vision v2 must import cleanly even if the new
# helper modules are not yet checked in; missing modules degrade to ``None``
# entries in the data report and the memo prompt handles their absence
# explicitly by naming them in the diligence section.
# ---------------------------------------------------------------------------

try:  # web search / news pack
    from app.exa import ExaClient, build_research_pack  # type: ignore
except ImportError:  # pragma: no cover - depends on parallel work
    ExaClient = None  # type: ignore[assignment]
    build_research_pack = None  # type: ignore[assignment]

try:  # multi-quarter SEC XBRL trend pack
    from app.sec_trend import build_sec_trend_pack  # type: ignore
except ImportError:  # pragma: no cover - depends on parallel work
    build_sec_trend_pack = None  # type: ignore[assignment]

try:  # torque indicator
    from app.torque import (  # type: ignore
        TorqueResult,
        compute_torque_score,
    )
except ImportError:  # pragma: no cover - depends on parallel work
    TorqueResult = None  # type: ignore[assignment]
    compute_torque_score = None  # type: ignore[assignment]

try:  # reclassification scoring (old noun / new verb)
    from app.reclassification import (  # type: ignore
        ReclassificationResult,
        score_reclassification,
    )
except ImportError:  # pragma: no cover - depends on parallel work
    ReclassificationResult = None  # type: ignore[assignment]
    score_reclassification = None  # type: ignore[assignment]

# Existing modules used to assemble the structured data report.
from app.market_data import MarketDataClient, clean_ticker
from app.sec import SecClient
from app.tools import build_stock_fax_data


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_VISION_V2_MAX_TOKENS = 8000
DEFAULT_VISION_V2_TEMPERATURE = 0.25

# The 13 section names emitted in the memo, in order. Used by both the prompt
# template and the section parser so they cannot drift apart.
VISION_V2_SECTIONS: tuple[str, ...] = (
    "Executive Read",
    "Old Noun → New Verb",
    "Hidden BOM Role",
    "Financial Bend",
    "Torque Math",
    "Reclassification Gap",
    "Proof Ladder Stage",
    "Catalysts (Near-term)",
    "Scenario Framework",
    "Kill Criteria",
    "Entry Discipline",
    "Diligence Agenda",
    "Final Rating + Target Price Band",
)


VISION_V2_SYSTEM = (
    "You are The Underlying's senior reclassification analyst. You write "
    "structured equity research memos in the 'misclassified revenue torque' "
    "framework: identify the old noun the market still uses to label a "
    "company, name the new verb it is actually executing, and quantify the "
    "torque between those two identities using the supplied multi-quarter "
    "XBRL trend, SEC source pack, Exa research pack, profile data, and "
    "technical history.\n\n"
    "Tone: institutional, sober, and evidence-based, but with conviction "
    "where the evidence supports it. You are writing for a serious "
    "buyside analyst who has read thousands of sellside notes and will "
    "discard anything that reads like hype, newsletter copy, or generic "
    "finance filler. Earn every adjective.\n\n"
    "Method:\n"
    "- Lead with the reclassification thesis. State the old noun (how the "
    "  market currently labels the company, including stale sector / "
    "  industry tags) and the new verb (what the cash flows, products, "
    "  and filings say the business is actually becoming).\n"
    "- Back every factual claim with an inline citation to the supplied "
    "  data. Use forms like '(SEC 10-Q Item 7, filed 2025-11-04)', "
    "  '(Exa: techcrunch.com, 2025-12-03)', '(XBRL Revenue, Q3 2025: "
    "  $126.5M)', '(Profile: Industry=Specialty Chemicals)'. Never cite a "
    "  field that is not present in the supplied JSON.\n"
    "- Compute torque math explicitly whenever XBRL trend data is "
    "  available. Show the arithmetic so the analyst can audit it: "
    "  latest revenue, gross margin, opex run-rate, the sensitivity of "
    "  operating income to revenue moves, and the implied per-share "
    "  earnings power at +10%, +20%, +30% revenue.\n"
    "- Propose specific target prices in a scenario table. Each target "
    "  must show its own revenue assumption, gross margin, EPS power, "
    "  multiple, and implied price. Do not write 'target ~$X' without "
    "  the math.\n"
    "- State kill criteria and proof-ladder stage. The kill criteria are "
    "  the specific observable events that would force you to abandon the "
    "  reclassification thesis. The proof ladder is the 0-5 scale of how "
    "  confirmed the new verb is today.\n\n"
    "Hard constraints:\n"
    "- Use only the supplied data. Never invent customers, products, "
    "  partners, peers, guidance, management quotes, contract values, "
    "  earnings dates, or macro facts. If a workstream is missing, name "
    "  it explicitly in the Diligence Agenda; do not paper over it.\n"
    "- Do not write generic catalysts ('earnings could be a catalyst', "
    "  'macro tailwinds', 'AI exposure'). Catalysts must be specific, "
    "  dated, and tied to either reclassification.catalysts or the "
    "  supplied SEC / Exa data.\n"
    "- Use exactly one rating from this scale at the bottom: Strong Buy, "
    "  Buy, Hold, Neutral, Sell, Strong Sell. Format it as 'Rating: "
    "  <X>. Target band: $low – $mid – $high.'\n"
    "- Maintain analyst quality throughout: no hype, no filler, no "
    "  hedging that conceals an actual view. Disagree with the consensus "
    "  explicitly when the data supports it; agree with the consensus "
    "  explicitly when the data supports that instead.\n\n"
    "Output format: markdown. Every section header must use the exact "
    "form '### N. SECTION NAME' so the downstream parser can split the "
    "memo into structured pages."
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def vision_v2_system_prompt() -> str:
    """Return the system prompt for the Vision v2 memo generator."""

    return VISION_V2_SYSTEM


def build_vision_v2_data(
    market_client: MarketDataClient,
    ticker: str,
    *,
    sec_client: SecClient | None = None,
    exa_client: Any | None = None,
) -> dict[str, Any]:
    """Pull all structured data needed for the Vision v2 memo.

    The returned dictionary is robust: when optional modules
    (``app.sec_trend``, ``app.exa``, ``app.torque``,
    ``app.reclassification``) are not available the corresponding entries
    are present but set to ``None`` or a short explanatory dict. The
    function never raises for missing optional data; only fundamental
    market data failures (which originate inside ``build_stock_fax_data``)
    will propagate.
    """

    symbol = clean_ticker(ticker)

    # 1. Base structured data (yfinance + SEC source pack + market data).
    base = build_stock_fax_data(market_client, symbol, sec_client=sec_client)

    # Pull raw history + profile separately for torque / reclassification,
    # both of which want the raw HistoryResult rather than the digested
    # base report.
    history_result = _safe_call(
        market_client.get_history,
        symbol,
        period="2y",
        label="History",
    )
    if isinstance(history_result, Mapping) and history_result.get("Status") == "error":
        # The fax build above already succeeded, so a second history fetch
        # failure should not block the memo — treat it as no history.
        history_result = None

    profile = _safe_call(market_client.get_profile, symbol, label="Profile")
    if isinstance(profile, Mapping) and profile.get("Status") == "error":
        profile = {}

    # 2. Multi-quarter SEC trend pack (optional, parallel module).
    sec_trend_pack = _safe_call(
        build_sec_trend_pack,
        sec_client,
        symbol,
        label="SEC Trend Pack",
    )

    # 3. Exa research pack (optional, parallel module).
    exa_pack = _safe_call(
        build_research_pack,
        exa_client,
        symbol,
        base.get("Name") or symbol,
        industry=base.get("Industry"),
        sector=base.get("Sector"),
        label="Exa Research Pack",
    )

    # 4. Torque score (optional, parallel module).
    market_cap_value = _market_cap_number(base.get("Snapshot"), profile)
    torque_payload = _safe_call(
        compute_torque_score,
        history=history_result if not isinstance(history_result, Mapping) else None,
        sec_trend=sec_trend_pack if isinstance(sec_trend_pack, dict) else None,
        profile=profile if isinstance(profile, dict) else None,
        market_cap=market_cap_value,
        label="Torque",
    )
    torque_dict = _to_plain_dict(torque_payload)

    # 5. Reclassification score (optional, parallel module).
    reclassification_payload = _safe_call(
        score_reclassification,
        ticker=symbol,
        profile=profile if isinstance(profile, dict) else None,
        history=history_result if not isinstance(history_result, Mapping) else None,
        sec_trend=sec_trend_pack if isinstance(sec_trend_pack, dict) else None,
        sec_source_pack=base.get("SEC Source Pack")
        if isinstance(base.get("SEC Source Pack"), dict)
        else None,
        exa_research=exa_pack if isinstance(exa_pack, dict) else None,
        torque_result=torque_dict if isinstance(torque_dict, dict) else None,
        label="Reclassification",
    )
    reclassification_dict = _to_plain_dict(reclassification_payload)

    history_summary = _history_summary(base)
    snapshot = base.get("Snapshot") or {}
    profile_summary = _profile_summary(base)

    report: dict[str, Any] = {
        "Ticker": symbol,
        "Name": base.get("Name"),
        "Sector": base.get("Sector"),
        "Industry": base.get("Industry"),
        "Profile": profile_summary,
        "Snapshot": snapshot,
        "History Summary": history_summary,
        "SEC Source Pack": base.get("SEC Source Pack"),
        "SEC Trend Pack": sec_trend_pack,
        "Earnings Source Pack": base.get("Earnings Source Pack"),
        "Exa Research Pack": exa_pack,
        "Torque": torque_dict,
        "Reclassification": reclassification_dict,
        "Export Rows": base.get("Export Rows", []),
    }

    # Condensed payload that gets embedded in the prompt. Keeps the JSON
    # readable for Claude and well under the context window.
    report["Memo Inputs"] = _memo_inputs(report)
    return report


def vision_v2_prompt(report: Mapping[str, Any]) -> str:
    """Return the user prompt for the Vision v2 memo.

    The prompt names every section in the exact order they must appear
    and embeds a JSON payload of the supplied data. ``Export Rows`` is
    omitted from the payload because it is large and not useful to the
    memo writer.
    """

    ticker = report.get("Ticker") or "the company"
    name = report.get("Name") or ticker

    payload = {key: value for key, value in report.items() if key != "Export Rows"}
    payload_json = json.dumps(payload, default=str, indent=2, sort_keys=True)

    sections_block = _sections_instruction_block()

    prompt = (
        f"Write a Vision v2 reclassification memo for {ticker} ({name}) in "
        "markdown. The memo must be ~2500-4500 words and read like the work "
        "of a senior buyside analyst at a fundamental long-short fund. "
        "Start with exactly the line:\n\n"
        f"# {ticker} Vision v2 — Reclassification Memo\n\n"
        "Then follow with the 13 sections below, in order, each introduced "
        "by a level-3 header of the form '### N. SECTION NAME'. Do not "
        "rename, reorder, merge, or skip sections; the downstream PDF "
        "renderer parses memos by splitting on those exact headers.\n\n"
        "Citation policy: every factual claim is followed by an inline "
        "citation to a supplied field. Use forms like '(SEC 10-Q Item 7, "
        "filed 2025-11-04)', '(Exa: techcrunch.com, 2025-12-03)', '(XBRL "
        "Revenue, Q3 2025: $126.5M)', '(Profile: Industry=Specialty "
        "Chemicals)'. Never cite a field that is absent. When a normal "
        "analyst workstream is missing (no SEC trend, no Exa pack, no "
        "torque data), call it out in the Diligence Agenda rather than "
        "inventing a substitute.\n\n"
        "Quantitative policy: when the SEC Trend Pack and Torque payload "
        "are present, do the arithmetic in the open. Show latest "
        "quarterly revenue, gross margin, opex run-rate, and the +10% / "
        "+20% / +30% revenue sensitivity on operating income and "
        "earnings power. When those packs are missing, say so in the "
        "Torque Math section and explain what the Diligence Agenda must "
        "produce to fill the gap.\n\n"
        "Rating policy: exactly one rating from this scale at the very "
        "end — Strong Buy, Buy, Hold, Neutral, Sell, Strong Sell — "
        "formatted as 'Rating: <X>. Target band: $low – $mid – "
        "$high.' followed by two sentences tying the rating to the "
        "strongest supporting evidence and the strongest disconfirming "
        "signal.\n\n"
        f"{sections_block}\n\n"
        "Supplied structured data follows as JSON. Use only this data; "
        "do not invent anything that is not here.\n\n"
        "```json\n"
        f"{payload_json}\n"
        "```\n"
    )
    return prompt


def stream_vision_v2_text(
    report: Mapping[str, Any],
    *,
    text_generator: TextGenerator | StreamingTextGenerator | None = None,
    api_key: str | None = None,
    text_model: str | None = None,
    session: requests.Session | None = None,
    max_tokens: int = DEFAULT_VISION_V2_MAX_TOKENS,
) -> Iterator[str]:
    """Stream Vision v2 memo tokens from Anthropic.

    Mirrors :func:`app.tools.stream_market_memo_text` but uses the Vision
    v2 system + user prompts and a larger max-tokens budget. Falls back
    to a single non-streaming call when the generator does not implement
    ``stream_text``.
    """

    generator = text_generator or AnthropicTextClient(
        api_key=api_key,
        model=text_model,
        session=session,
    )
    system = vision_v2_system_prompt()
    prompt = vision_v2_prompt(report)

    stream_text = getattr(generator, "stream_text", None)
    if callable(stream_text):
        yield from stream_text(
            system=system,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=DEFAULT_VISION_V2_TEMPERATURE,
        )
        return

    generated = generator.generate_text(
        system=system,
        prompt=prompt,
        max_tokens=max_tokens,
        temperature=DEFAULT_VISION_V2_TEMPERATURE,
    )
    yield generated.text


def generate_vision_v2_text(
    report: Mapping[str, Any],
    *,
    text_generator: TextGenerator | None = None,
    api_key: str | None = None,
    text_model: str | None = None,
    session: requests.Session | None = None,
    max_tokens: int = DEFAULT_VISION_V2_MAX_TOKENS,
) -> GeneratedText:
    """Synchronously generate the Vision v2 memo. Used by orchestrator."""

    generator = text_generator or AnthropicTextClient(
        api_key=api_key,
        model=text_model,
        session=session,
    )
    return generator.generate_text(
        system=vision_v2_system_prompt(),
        prompt=vision_v2_prompt(report),
        max_tokens=max_tokens,
        temperature=DEFAULT_VISION_V2_TEMPERATURE,
    )


def build_vision_v2_memo(
    market_client: MarketDataClient,
    ticker: str,
    *,
    sec_client: SecClient | None = None,
    exa_client: Any | None = None,
    text_generator: TextGenerator | None = None,
    api_key: str | None = None,
    text_model: str | None = None,
    session: requests.Session | None = None,
    max_tokens: int = DEFAULT_VISION_V2_MAX_TOKENS,
) -> dict[str, Any]:
    """Build the Vision v2 structured data and generate the memo.

    Returns the data report plus the generated memo text, model metadata,
    and a parsed ``Memo Sections`` dict keyed by section name.
    """

    report = build_vision_v2_data(
        market_client,
        ticker,
        sec_client=sec_client,
        exa_client=exa_client,
    )
    generated = generate_vision_v2_text(
        report,
        text_generator=text_generator,
        api_key=api_key,
        text_model=text_model,
        session=session,
        max_tokens=max_tokens,
    )
    sections = parse_memo_sections(generated.text)
    return {
        **report,
        "Memo Text": generated.text,
        "Text Provider": generated.provider,
        "Text Model": generated.model,
        "Memo Sections": sections,
    }


# ---------------------------------------------------------------------------
# Memo Sections parser
# ---------------------------------------------------------------------------


_SECTION_HEADER_RE = re.compile(
    r"^\s*###\s+\d+\.\s+(?P<name>.+?)\s*$",
    re.MULTILINE,
)


def parse_memo_sections(memo_text: str) -> dict[str, str]:
    """Split the memo into a dict of ``{section_name: body}``.

    Headers are identified by lines matching ``### N. NAME`` (level-3
    markdown header followed by a number, a period, and the section
    name). The order of insertion is preserved.

    Any content that appears before the first matching header is kept
    under the ``Preamble`` key so the PDF renderer can decide what to do
    with it.
    """

    if not isinstance(memo_text, str) or not memo_text.strip():
        return {}

    sections: dict[str, str] = {}
    matches = list(_SECTION_HEADER_RE.finditer(memo_text))
    if not matches:
        return {"Preamble": memo_text.strip()}

    first = matches[0]
    if first.start() > 0:
        preamble = memo_text[: first.start()].strip()
        if preamble:
            sections["Preamble"] = preamble

    for index, match in enumerate(matches):
        name = match.group("name").strip()
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(memo_text)
        body = memo_text[body_start:body_end].strip()
        sections[name] = body

    return sections


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_call(callable_: Any, *args: Any, label: str = "", **kwargs: Any) -> Any:
    """Call ``callable_`` if available, else return ``None``.

    Swallows ``Exception`` so a flaky optional data source cannot break
    memo generation. Errors are surfaced as ``{"Status": "error",
    "Errors": [str(exc)]}`` so the prompt can still flag them in the
    diligence agenda.
    """

    if callable_ is None:
        return None
    try:
        return callable_(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - defensive
        return {
            "Status": "error",
            "Provider": label or callable_.__name__,
            "Errors": [f"{type(exc).__name__}: {exc}"],
        }


def _to_plain_dict(value: Any) -> Any:
    """Coerce dataclasses (e.g. ``TorqueResult``) to plain dicts."""

    if value is None:
        return None
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "asdict") and callable(value.asdict):
        try:
            return value.asdict()
        except Exception:  # noqa: BLE001
            return value
    return value


def _profile_summary(base: Mapping[str, Any]) -> dict[str, Any]:
    """Condensed profile / business context block for the prompt."""

    business_context = base.get("Business Context") or {}
    if not isinstance(business_context, Mapping):
        business_context = {}

    return {
        "Company": business_context.get("Company") or base.get("Name"),
        "Sector": business_context.get("Sector") or base.get("Sector"),
        "Industry": business_context.get("Industry") or base.get("Industry"),
        "Country": business_context.get("Country"),
        "Website": business_context.get("Website"),
        "Employees": business_context.get("Employees"),
        "Business Summary": business_context.get("Business Summary"),
    }


def _history_summary(base: Mapping[str, Any]) -> dict[str, Any]:
    """Condense the performance and signal blocks into the fields the
    Entry Discipline section relies on (RSI, 50DMA distance, 52W
    distance, multi-period returns)."""

    performance = base.get("Performance Metrics") or {}
    signals = base.get("Signal Summary") or {}
    snapshot = base.get("Snapshot") or {}

    summary: dict[str, Any] = {
        "Latest Close": _safe_number(performance.get("Latest Price"))
        or _safe_number(snapshot.get("Price")),
        "Distance From 52W High (%)": _safe_number(performance.get("Distance From 52W High (%)")),
        "Distance From 52W Low (%)": _safe_number(performance.get("Distance From 52W Low (%)")),
    }

    for label in ("1W", "1M", "3M", "6M", "1Y", "2Y"):
        window = performance.get(label)
        if isinstance(window, Mapping):
            summary[f"{label} Return (%)"] = _safe_number(window.get("Return (%)"))

    # Pull anything that looks like an RSI / trend / DMA signal from the
    # signal summary; we don't enforce key names because Vision v1 may
    # rename them.
    for key, value in signals.items() if isinstance(signals, Mapping) else []:
        if not isinstance(key, str):
            continue
        lowered = key.lower()
        if any(token in lowered for token in ("rsi", "dma", "ema", "trend", "regime", "vol")):
            summary[key] = _safe_number(value) if isinstance(value, (int, float)) else value

    return summary


def _memo_inputs(report: Mapping[str, Any]) -> dict[str, Any]:
    """Compact, prompt-friendly view of the data report.

    The full report is still passed to the prompt, but ``Memo Inputs``
    gives Claude a fast index of "what do we actually have" so it can
    open the memo without sifting through nested JSON.
    """

    def _present(value: Any) -> str:
        if value is None:
            return "missing"
        if isinstance(value, Mapping):
            status = value.get("Status")
            if isinstance(status, str) and status.lower() in {
                "not configured",
                "unavailable",
                "error",
                "missing",
            }:
                return status
            return "present" if value else "missing"
        if isinstance(value, (list, tuple)):
            return "present" if value else "missing"
        return "present"

    history_summary = report.get("History Summary") or {}
    snapshot = report.get("Snapshot") or {}
    torque = report.get("Torque") or {}
    reclassification = report.get("Reclassification") or {}

    return {
        "Ticker": report.get("Ticker"),
        "Name": report.get("Name"),
        "Sector": report.get("Sector"),
        "Industry": report.get("Industry"),
        "Price": snapshot.get("Price") if isinstance(snapshot, Mapping) else None,
        "Market Cap": snapshot.get("Market Cap") if isinstance(snapshot, Mapping) else None,
        "Trailing PE": snapshot.get("Trailing PE") if isinstance(snapshot, Mapping) else None,
        "Beta": snapshot.get("Beta") if isinstance(snapshot, Mapping) else None,
        "52W High": snapshot.get("52W High") if isinstance(snapshot, Mapping) else None,
        "52W Low": snapshot.get("52W Low") if isinstance(snapshot, Mapping) else None,
        "Latest Close": history_summary.get("Latest Close"),
        "3M Return (%)": history_summary.get("3M Return (%)"),
        "6M Return (%)": history_summary.get("6M Return (%)"),
        "1M Return (%)": history_summary.get("1M Return (%)"),
        "Data Availability": {
            "SEC Source Pack": _present(report.get("SEC Source Pack")),
            "SEC Trend Pack": _present(report.get("SEC Trend Pack")),
            "Earnings Source Pack": _present(report.get("Earnings Source Pack")),
            "Exa Research Pack": _present(report.get("Exa Research Pack")),
            "Torque": _present(report.get("Torque")),
            "Reclassification": _present(report.get("Reclassification")),
        },
        "Torque Headline": _torque_headline(torque),
        "Reclassification Headline": _reclassification_headline(reclassification),
    }


def _market_cap_number(snapshot: Any, profile: Any) -> float | None:
    """Best-effort extraction of a numeric market cap for the torque
    indicator. Snapshot stores it as a compact string ('5.00B') so we
    prefer the raw profile field when available."""

    if isinstance(profile, Mapping):
        raw = profile.get("marketCap")
        if isinstance(raw, (int, float)) and raw == raw:
            return float(raw)
    if isinstance(snapshot, Mapping):
        raw = snapshot.get("Market Cap")
        if isinstance(raw, (int, float)) and raw == raw:
            return float(raw)
    return None


def _safe_number(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        try:
            if value != value:  # NaN
                return None
        except Exception:  # noqa: BLE001
            return None
        return float(value)
    return value


def _torque_headline(torque: Any) -> dict[str, Any] | None:
    if not isinstance(torque, Mapping) or not torque:
        return None
    candidate_keys = (
        "score",
        "Score",
        "torque_score",
        "summary",
        "Summary",
        "rating",
        "Rating",
        "label",
        "Label",
    )
    headline: dict[str, Any] = {}
    for key in candidate_keys:
        if key in torque and torque[key] is not None:
            headline[key] = torque[key]
    return headline or None


def _reclassification_headline(reclassification: Any) -> dict[str, Any] | None:
    if not isinstance(reclassification, Mapping) or not reclassification:
        return None
    headline: dict[str, Any] = {}
    for key in (
        "old_noun",
        "new_verb",
        "score",
        "stage",
        "proof_ladder_stage",
        "summary",
        "Summary",
        "thesis",
    ):
        if key in reclassification and reclassification[key] is not None:
            headline[key] = reclassification[key]
    return headline or None


def _sections_instruction_block() -> str:
    """Render the explicit, numbered section guide that goes into the prompt.

    The numbering is canonical and the renderer relies on it; the helper
    text under each header is the spec for what that section must
    contain, derived from the analyst playbook.
    """

    items = [
        (
            "Executive Read",
            "3 paragraphs. Provisional rating, the reclassification claim "
            "in one sentence (old noun → new verb), what matters most, "
            "strongest counterargument, and your confidence level.",
        ),
        (
            "Old Noun → New Verb",
            "1-2 paragraphs. Explicit statement of how the market currently "
            "labels this company (cite Profile.Industry, Profile.Sector, and "
            "SEC Business section) versus what the cash flows, filings, and "
            "Exa-sourced news indicate it is actually becoming.",
        ),
        (
            "Hidden BOM Role",
            "1 paragraph. What specific function does this company perform "
            "in the bill-of-materials of the new AI / compute / energy / "
            "grid economy? Be concrete about the workflow step it owns.",
        ),
        (
            "Financial Bend",
            "2-3 paragraphs. Walk through the multi-quarter XBRL trend from "
            "SEC Trend Pack. Revenue trajectory, gross margin trend, "
            "operating margin, operating leverage. Use specific numbers and "
            "cite each quarter inline.",
        ),
        (
            "Torque Math",
            "Explicit numbered calculation or table. Latest quarterly "
            "revenue, gross margin, opex run-rate, sensitivity at +15% "
            "revenue, then annualized EPS power at +10% / +20% / +30%. "
            "Show the arithmetic. If SEC Trend Pack is missing, say so and "
            "move the math request to the Diligence Agenda.",
        ),
        (
            "Reclassification Gap",
            "1-2 paragraphs. Old peer group with its stale multiple, new "
            "peer group implied by the new verb with its multiple. What "
            "multiple expansion is plausible if the reclassification "
            "completes?",
        ),
        (
            "Proof Ladder Stage",
            "1 paragraph. Where on the 0-5 proof ladder is this name today "
            "and why? What specific proof would advance it to the next "
            "rung?",
        ),
        (
            "Catalysts (Near-term)",
            "Bulleted list of 3-6 items. Pull from reclassification."
            "catalysts and supplied SEC / Exa events. Each catalyst is "
            "specific, dated when possible, and tied to a citation.",
        ),
        (
            "Scenario Framework",
            "A markdown table with columns: Scenario | Revenue assumption "
            "| Gross margin | EPS power | Multiple | Implied price. Rows: "
            "Bear, Base, Bull. Below the table, 2-3 sentences on how to "
            "weight scenario probabilities.",
        ),
        (
            "Kill Criteria",
            "Bulleted list of 3-5 items from reclassification.kill_"
            "criteria. Each item is observable, dated, and disqualifying.",
        ),
        (
            "Entry Discipline",
            "1 paragraph. Use History Summary (RSI, 50DMA distance, 52W "
            "distance, 3M/6M returns) to comment on whether the stock is "
            "basing, breaking out, extended, or digesting, and where you "
            "would actually buy.",
        ),
        (
            "Diligence Agenda",
            "Bulleted list of missing or weak workstreams from "
            "reclassification.diligence_gaps plus any obvious SEC / "
            "transcript / Exa gaps the supplied data flagged as missing.",
        ),
        (
            "Final Rating + Target Price Band",
            "1-2 sentences. Exactly one rating: Strong Buy / Buy / Hold / "
            "Neutral / Sell / Strong Sell. Format: 'Rating: <X>. Target "
            "band: $low – $mid – $high.' Follow with two "
            "sentences tying the rating to the strongest evidence and "
            "the strongest disconfirming signal.",
        ),
    ]
    lines = ["Sections (use these exact headers, numbered, in this order):"]
    for index, (name, guidance) in enumerate(items, start=1):
        lines.append(f"### {index}. {name}")
        lines.append(f"   {guidance}")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_VISION_V2_MAX_TOKENS",
    "DEFAULT_VISION_V2_TEMPERATURE",
    "VISION_V2_SECTIONS",
    "VISION_V2_SYSTEM",
    "build_vision_v2_data",
    "build_vision_v2_memo",
    "generate_vision_v2_text",
    "parse_memo_sections",
    "stream_vision_v2_text",
    "vision_v2_prompt",
    "vision_v2_system_prompt",
]
