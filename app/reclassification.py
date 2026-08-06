"""Old Noun / New Verb reclassification analysis.

Deterministic, rules-based scoring that classifies a company by its *functional*
role in the AI / compute / energy / grid economy rather than by its stale
SIC-style category label. The structured output is consumed by the memo LLM
and is also suitable for screening.

The MXL motivating example:
    Old noun: "broadband / analog / mixed-signal semiconductor"
    New verb: "move AI traffic optically"
    Hidden BOM role: "PAM4 DSP and TIA supplier in optical module BOM"
    Functional layer: "Nerves"
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from app.market_data import HistoryResult

# ---------------------------------------------------------------------------
# Theme dictionary
# ---------------------------------------------------------------------------

THEME_KEYWORDS: dict[str, dict[str, Any]] = {
    "AI_COMPUTE": {
        "verb": "supply the silicon and platforms that train and run AI",
        "layer": "Brain",
        "keywords": [
            "gpu",
            "accelerator",
            "ai accelerator",
            "training",
            "inference",
            "data center",
            "data-center",
            "ai data center",
            "ai factory",
            "ai factories",
            "foundation model",
            "large language model",
            "llm",
            "cuda",
            "hopper",
            "blackwell",
            "tensor",
            "ai server",
            "ai platform",
            "ai infrastructure",
            "ai compute",
            "ai workloads",
            "ai workload",
            "hyperscaler",
            "hyperscale",
            "scale-up",
            "scale-out",
            "supercomputer",
            "deep learning",
            "machine learning",
            "neural network",
            "model training",
        ],
        "hidden_bom_roles": [
            "AI training GPU / accelerator",
            "AI inference accelerator",
            "AI server platform",
            "data-center compute reference design",
            "AI software / runtime",
        ],
    },
    "AI_NETWORKING": {
        "verb": "switch AI traffic between accelerators",
        "layer": "Nerves",
        "keywords": [
            "ai network",
            "ai networking",
            "ethernet",
            "infiniband",
            "switching",
            "switch fabric",
            "tomahawk",
            "spectrum-x",
            "spectrum x",
            "nvlink",
            "smartnic",
            "dpu",
            "interconnect",
            "ai cluster",
            "back-end network",
            "scale-out fabric",
            "data center switch",
            "data-center switch",
        ],
        "hidden_bom_roles": [
            "AI cluster switch ASIC",
            "scale-up interconnect (NVLink-class)",
            "scale-out fabric switch",
            "SmartNIC / DPU",
        ],
    },
    "AI_OPTICS": {
        "verb": "move AI traffic optically",
        "layer": "Nerves",
        "keywords": [
            "400g",
            "800g",
            "1.6t",
            "3.2t",
            "pam4",
            "dsp",
            "tia",
            "optical",
            "optics",
            "optical module",
            "transceiver",
            "transceivers",
            "coherent",
            "silicon photonics",
            "co-packaged optics",
            "linear pluggable optics",
            "lpo",
            "data-center interconnect",
            "data center interconnect",
            "metro optical",
            "scale-up",
            "scale-out",
            "hyperscale",
            "active optical cable",
            "aec",
        ],
        "hidden_bom_roles": [
            "PAM4 DSP",
            "TIA / driver IC",
            "optical transceiver",
            "retimer",
            "active electrical cable",
            "high-speed connector",
        ],
    },
    "AI_POWER": {
        "verb": "power AI compute loads",
        "layer": "Blood",
        "keywords": [
            "data center power",
            "data-center power",
            "switchgear",
            "transformer",
            "ups",
            "busbar",
            "power module",
            "power conversion",
            "ai data center",
            "data-center capex",
            "data center capex",
            "large load",
            "gigawatt",
            "interconnection",
            "behind-the-meter",
            "powered land",
            "powered shell",
            "campus",
            "grid",
            "load growth",
        ],
        "hidden_bom_roles": [
            "medium-voltage switchgear",
            "data-center UPS",
            "power distribution module",
            "transformer",
            "busbar",
        ],
    },
    "AI_COOLING": {
        "verb": "cool dense AI compute",
        "layer": "Skin",
        "keywords": [
            "liquid cooling",
            "immersion",
            "rear-door heat exchanger",
            "cold plate",
            "thermal",
            "cdu",
            "air handler",
        ],
        "hidden_bom_roles": [
            "cold plate",
            "coolant distribution unit",
            "rear-door heat exchanger",
            "immersion tank",
            "thermal management subsystem",
        ],
    },
    "AI_MEMORY": {
        "verb": "expand AI memory and cache",
        "layer": "Memory",
        "keywords": [
            "cxl",
            "hbm",
            "high-bandwidth memory",
            "high bandwidth memory",
            "hbm3",
            "hbm3e",
            "hbm4",
            "kv cache",
            "memory pooling",
            "nvme",
            "storage accelerator",
            "ssd controller",
            "qlc",
            "ddr5",
            "graphics memory",
            "memory bandwidth",
        ],
        "hidden_bom_roles": [
            "CXL controller",
            "SSD controller",
            "memory expander",
            "HBM stack supplier",
            "NVMe accelerator",
        ],
    },
    "AI_PACKAGING": {
        "verb": "package and test AI silicon",
        "layer": "Skin",
        "keywords": [
            "advanced packaging",
            "fan-out",
            "2.5d",
            "chiplet",
            "substrate",
            "co-packaged optics",
            "test socket",
            "probe card",
            "wafer test",
        ],
        "hidden_bom_roles": [
            "advanced substrate",
            "probe card",
            "test socket",
            "co-packaged optics module",
            "chiplet interconnect",
        ],
    },
    "AI_EDGE": {
        "verb": "run AI inference at the edge",
        "layer": "Brain (edge)",
        "keywords": [
            "npu",
            "on-device ai",
            "edge inference",
            "embedded ai",
            "sensor fusion",
            "secure enclave",
            "ai pc",
        ],
        "hidden_bom_roles": [
            "edge NPU",
            "secure enclave",
            "sensor-fusion SoC",
            "AI PC accelerator",
        ],
    },
    "AI_COHERENCE": {
        "verb": "orchestrate distributed cognition",
        "layer": "Coherence layer",
        "keywords": [
            "agent orchestration",
            "identity",
            "permissioning",
            "audit",
            "rollback",
            "observability",
            "workflow state",
            "tool governance",
            "memory governance",
        ],
        "hidden_bom_roles": [
            "agent orchestration runtime",
            "identity and permissioning service",
            "observability and audit layer",
            "workflow-state store",
        ],
    },
    "GRID_BUILDOUT": {
        "verb": "expand and modernize the electric grid",
        "layer": "Blood (grid)",
        "keywords": [
            "transmission",
            "substation",
            "interconnection queue",
            "transformer",
            "load growth",
            "renewable interconnect",
            "grid resilience",
        ],
        "hidden_bom_roles": [
            "transmission equipment",
            "substation automation",
            "grid transformer",
            "interconnect protection relay",
        ],
    },
    "ROBOTICS": {
        "verb": "actuate physical work",
        "layer": "Muscle",
        "keywords": [
            "robotics",
            "actuator",
            "servo",
            "humanoid",
            "warehouse automation",
            "agv",
            "autonomous mobile",
        ],
        "hidden_bom_roles": [
            "precision actuator",
            "servo drive",
            "humanoid joint module",
            "AMR navigation stack",
        ],
    },
}

STALE_OLD_NOUN_INDUSTRIES: dict[str, str] = {
    "Semiconductors": "broadband / analog / mixed-signal semiconductor",
    "Communication Equipment": "communication equipment vendor",
    "Electronic Components": "passive electronic components supplier",
    "Industrial Electrical Equipment": "industrial electrical equipment maker",
    "Specialty Industrial Machinery": "specialty industrial machinery vendor",
    "Engineering & Construction": "engineering and construction contractor",
    "Electrical Equipment & Parts": "electrical equipment supplier",
    "Utilities - Regulated Electric": "regulated electric utility",
    "REIT - Industrial": "industrial REIT",
    "Heavy Construction": "heavy construction contractor",
    "Computer Hardware": "legacy computer hardware vendor",
    "Telecom Services": "incumbent telecom carrier",
    "Aerospace & Defense": "aerospace and defense contractor",
    "Semiconductor Equipment & Materials": "semi capital equipment supplier",
    "Storage": "storage hardware vendor",
}

PROOF_STAGE_LABELS: dict[int, str] = {
    0: "Narrative only",
    1: "Product proof",
    2: "Financial bend",
    3: "Proof / guide-up",
    4: "Public renaming",
    5: "Overbuild / fatigue",
}

DEFAULT_VERB = "modernize infrastructure"
DEFAULT_LAYER = "Unclassified"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReclassificationResult:
    old_noun: str
    new_verb_candidates: list[dict[str, Any]] = field(default_factory=list)
    primary_new_verb: str = DEFAULT_VERB
    hidden_bom_role: str = ""
    functional_layer: str = DEFAULT_LAYER
    reclassification_gap: float = 0.0
    proof_stage: int = 0
    proof_stage_label: str = PROOF_STAGE_LABELS[0]
    target_low: float | None = None
    target_mid: float | None = None
    target_high: float | None = None
    target_basis: str = ""
    catalysts: list[str] = field(default_factory=list)
    kill_criteria: list[str] = field(default_factory=list)
    diligence_gaps: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, str):
        return ()
    return value if isinstance(value, Sequence) else ()


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _coalesce_text(*parts: Any) -> str:
    out: list[str] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, str):
            out.append(part)
        else:
            out.append(str(part))
    return " ".join(out)


def _section_text(section: Any) -> str:
    """Extract text from a section payload tolerantly.

    Sections coming from `app.sec` use the key ``Snippet`` rather than ``text``.
    The contract example shows ``.text``; support both, plus already-stringified
    sections.
    """

    if section is None:
        return ""
    if isinstance(section, str):
        return section
    if isinstance(section, Mapping):
        for key in ("Snippet", "snippet", "text", "Text", "Body", "body"):
            value = section.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _exa_bucket_text(bucket: Any) -> str:
    parts: list[str] = []
    for item in _as_sequence(bucket):
        if not isinstance(item, Mapping):
            continue
        for key in ("title", "snippet", "text"):
            value = item.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
    return " ".join(parts)


def _assemble_corpus(
    profile: Mapping[str, Any],
    sec_source_pack: Mapping[str, Any],
    exa_research: Mapping[str, Any] | None,
) -> str:
    parts: list[str] = []
    summary = profile.get("longBusinessSummary")
    if isinstance(summary, str):
        parts.append(summary)

    # Tolerate both the contract's "SEC Sections" key and the actual
    # "Filing Sections" key used by app.sec.
    sections = _as_mapping(sec_source_pack.get("SEC Sections")) or _as_mapping(
        sec_source_pack.get("Filing Sections")
    )
    for label in ("Business", "MD&A", "Risk Factors"):
        section = sections.get(label)
        text = _section_text(section)
        if not text:
            continue
        if label == "Risk Factors":
            text = text[:2000]
        parts.append(text)

    if exa_research:
        status = exa_research.get("Status")
        if status != "not configured":
            queries = _as_mapping(exa_research.get("Queries"))
            for bucket_name in ("product_and_customer", "language_mutation"):
                parts.append(_exa_bucket_text(queries.get(bucket_name)))

    return " ".join(part for part in parts if part).lower()


_WORD_BOUNDARY_RE = re.compile(r"[a-z0-9]")


@lru_cache(maxsize=2048)
def _boundary_pattern(kw: str) -> re.Pattern[str]:
    """Compile (and memoize) the word-boundary regex for a single-token keyword.

    The keyword set is fixed across requests, so compiling each pattern once and
    reusing it avoids re-running ``re.escape`` + recompilation on every ticker's
    corpus scan. Behavior is identical to the previous inline ``re.search``.
    """

    return re.compile(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])")


def _keyword_hit(corpus: str, keyword: str) -> bool:
    """Substring match with light word-boundary heuristics.

    For tokens containing letters or digits we want surrounding chars to be
    non-alphanumeric on each side to avoid e.g. "ups" matching "groups".
    Keywords containing spaces / hyphens / dots are matched as plain substrings.
    """

    if not keyword:
        return False
    kw = keyword.lower()
    if not corpus:
        return False
    if any(ch in kw for ch in (" ", "-", ".", "/")):
        return kw in corpus

    return _boundary_pattern(kw).search(corpus) is not None


def _score_theme(corpus: str, theme_def: Mapping[str, Any]) -> tuple[int, list[str]]:
    keywords = _as_sequence(theme_def.get("keywords"))
    matched: list[str] = []
    for raw in keywords:
        keyword = str(raw)
        if _keyword_hit(corpus, keyword):
            matched.append(keyword)
    return len(matched), matched


def _resolve_old_noun(profile: Mapping[str, Any]) -> str:
    industry = profile.get("industry")
    if isinstance(industry, str) and industry in STALE_OLD_NOUN_INDUSTRIES:
        return STALE_OLD_NOUN_INDUSTRIES[industry]
    if isinstance(industry, str) and industry.strip():
        return industry.strip()
    sector = profile.get("sector")
    if isinstance(sector, str) and sector.strip():
        return sector.strip()
    return "unspecified"


def _rank_themes(corpus: str) -> list[dict[str, Any]]:
    """Return all themes ranked by evidence_count desc, including zero-score."""

    scored: list[dict[str, Any]] = []
    for theme_id, theme_def in THEME_KEYWORDS.items():
        count, matched = _score_theme(corpus, theme_def)
        scored.append(
            {
                "theme_id": theme_id,
                "verb": str(theme_def["verb"]),
                "layer": str(theme_def["layer"]),
                "keywords": list(theme_def.get("keywords", [])),
                "hidden_bom_roles": list(theme_def.get("hidden_bom_roles", [])),
                "evidence_count": count,
                "matched_keywords": matched,
                "themes": [theme_id.lower()],
            }
        )
    scored.sort(key=lambda x: (-int(x["evidence_count"]), str(x["theme_id"])))
    return scored


def _public_candidates(ranked: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for theme in ranked[:3]:
        out.append(
            {
                "verb": theme["verb"],
                "evidence_count": int(theme["evidence_count"]),
                "themes": list(theme["themes"]),
                "matched_keywords": list(theme.get("matched_keywords", [])),
            }
        )
    return out


def _hidden_bom_role(
    corpus: str,
    top_theme: Mapping[str, Any] | None,
    functional_layer: str,
) -> str:
    if top_theme is None or int(top_theme.get("evidence_count", 0)) == 0:
        return (
            f"specialty supplier in the {functional_layer.lower()} layer of the "
            "AI infrastructure stack"
        )

    roles = list(top_theme.get("hidden_bom_roles") or [])
    for role in roles:
        if _keyword_hit(corpus, role.lower()):
            return role
    if roles:
        return roles[0]
    return (
        f"specialty supplier in the {functional_layer.lower()} layer of the "
        "AI infrastructure stack"
    )


# ---------------------------------------------------------------------------
# SEC trend extraction
# ---------------------------------------------------------------------------


def _sec_trend_signals(sec_trend: Mapping[str, Any]) -> dict[str, Any]:
    """Pull the handful of fields we use from a sec_trend pack.

    The contract uses ``Revenue Acceleration``. We also probe lower-cased
    alternates so synthetic test inputs stay compact.
    """

    out: dict[str, Any] = {
        "status": str(sec_trend.get("Status") or sec_trend.get("status") or "available"),
        "revenue_yoy": None,
        "accelerating": False,
        "latest_revenue": None,
        "gross_margin": None,
        "opex_run_rate": None,
        "shares_diluted": None,
        "op_leverage": None,
        "segments": None,
    }

    acc = _as_mapping(sec_trend.get("Revenue Acceleration")) or _as_mapping(
        sec_trend.get("revenue_acceleration")
    )
    if acc:
        out["revenue_yoy"] = _as_float(
            acc.get("yoy") or acc.get("YoY") or acc.get("yoy_growth")
        )
        out["accelerating"] = bool(acc.get("accelerating"))
    if out["revenue_yoy"] is None:
        out["revenue_yoy"] = _as_float(sec_trend.get("Revenue YoY"))

    out["latest_revenue"] = _as_float(
        sec_trend.get("Latest Revenue")
        or sec_trend.get("latest_revenue")
        or sec_trend.get("Revenue")
    )
    out["gross_margin"] = _as_float(
        sec_trend.get("Gross Margin")
        or sec_trend.get("gross_margin")
        or sec_trend.get("GrossMargin")
    )
    out["opex_run_rate"] = _as_float(
        sec_trend.get("Opex Run Rate")
        or sec_trend.get("opex_run_rate")
        or sec_trend.get("Operating Expenses")
    )
    out["shares_diluted"] = _as_float(
        sec_trend.get("Shares Diluted")
        or sec_trend.get("shares_diluted")
        or sec_trend.get("Diluted Shares")
    )
    out["op_leverage"] = sec_trend.get("Operating Leverage") or sec_trend.get("op_leverage")
    out["segments"] = sec_trend.get("Segments") or sec_trend.get("segments")
    return out


def _has_segment_data(signals: Mapping[str, Any]) -> bool:
    segments = signals.get("segments")
    if isinstance(segments, Mapping) and segments:
        return True
    if isinstance(segments, list) and segments:
        return True
    return False


# ---------------------------------------------------------------------------
# Price / perf
# ---------------------------------------------------------------------------


def _last_close(history: HistoryResult | None) -> float | None:
    if history is None:
        return None
    try:
        data = history.data
    except AttributeError:
        return None
    if data is None or getattr(data, "empty", True):
        return None
    if "Close" not in data.columns:
        return None
    try:
        return float(data["Close"].iloc[-1])
    except (IndexError, ValueError, TypeError):
        return None


def _trailing_return(history: HistoryResult | None, *, days: int) -> float | None:
    if history is None:
        return None
    data = getattr(history, "data", None)
    if data is None or getattr(data, "empty", True) or "Close" not in data.columns:
        return None
    closes = data["Close"]
    if len(closes) < 2:
        return None
    last = float(closes.iloc[-1])
    look = -min(days, len(closes))
    try:
        ref = float(closes.iloc[look])
    except (IndexError, ValueError, TypeError):
        return None
    if not ref:
        return None
    return (last - ref) / ref


# ---------------------------------------------------------------------------
# Gap + proof stage
# ---------------------------------------------------------------------------


def _reclassification_gap(
    *,
    old_noun: str,
    top_theme: Mapping[str, Any] | None,
    sec_signals: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> float:
    gap = 0.0
    if old_noun in STALE_OLD_NOUN_INDUSTRIES.values():
        gap += 0.3
    if top_theme is not None and int(top_theme.get("evidence_count", 0)) >= 5:
        gap += 0.4
    yoy = sec_signals.get("revenue_yoy")
    if isinstance(yoy, (int, float)) and float(yoy) > 0.25:
        gap += 0.2
    market_cap = _as_float(profile.get("marketCap"))
    if market_cap is not None and market_cap < 5_000_000_000:
        gap += 0.1
    if sec_signals.get("status") not in ("available", "partial"):
        gap = max(0.0, gap - 0.2)
    return max(0.0, min(1.0, gap))


def _proof_stage(
    *,
    top_theme: Mapping[str, Any] | None,
    sec_signals: Mapping[str, Any],
    profile: Mapping[str, Any],
    exa_research: Mapping[str, Any] | None,
    history: HistoryResult | None,
) -> int:
    evidence = int(top_theme.get("evidence_count", 0)) if top_theme else 0
    yoy = sec_signals.get("revenue_yoy")
    yoy_f = float(yoy) if isinstance(yoy, (int, float)) else None
    accelerating = bool(sec_signals.get("accelerating"))
    op_leverage_raw = sec_signals.get("op_leverage")
    op_leverage = str(op_leverage_raw).lower() if op_leverage_raw else ""
    summary = str(profile.get("longBusinessSummary") or "").lower()

    stages_met: list[int] = []

    if evidence >= 1:
        stages_met.append(0)

    has_product_mention = any(
        token in summary for token in ("customer", "product", "platform", "module", "deployed")
    )
    if evidence >= 3 and has_product_mention:
        stages_met.append(1)

    if accelerating and yoy_f is not None and yoy_f > 0.15:
        stages_met.append(2)

    if yoy_f is not None and yoy_f > 0.30 and op_leverage in ("high", "moderate"):
        stages_met.append(3)

    sell_side_text = ""
    if exa_research:
        queries = _as_mapping(exa_research.get("Queries"))
        sell_side_text = _exa_bucket_text(queries.get("sell_side_framing")).lower()

    theme_keywords = list(top_theme.get("keywords") or []) if top_theme else []
    sell_side_mentions = sum(
        1 for kw in theme_keywords if _keyword_hit(sell_side_text, str(kw).lower())
    )
    perf_3m = _trailing_return(history, days=63)
    if sell_side_mentions > 4 or (perf_3m is not None and perf_3m > 0.40):
        stages_met.append(4)

    perf_1m = _trailing_return(history, days=21)
    rev_decel = (yoy_f is not None and yoy_f < 0.05) or (
        accelerating is False and yoy_f is not None and yoy_f < 0.10
    )
    if (perf_1m is not None and perf_1m < -0.10 and 4 in stages_met) or rev_decel:
        # Only call stage 5 if we have evidence of a prior run-up + reversal
        # (stage 4 met) or a clear deceleration after at least narrative.
        if 4 in stages_met or 2 in stages_met:
            stages_met.append(5)

    # Cap progression: don't jump from 0 to 3 without intermediate signals.
    if not stages_met:
        return 0
    stages_met_sorted = sorted(set(stages_met))
    highest = 0
    for stage in stages_met_sorted:
        if stage <= highest + 1 or stage in (4, 5):
            highest = max(highest, stage)
    # If sec_trend is missing/unavailable, hold stage low.
    if sec_signals.get("status") not in ("available", "partial") and highest > 1:
        highest = 1
    return highest


# ---------------------------------------------------------------------------
# Target prices
# ---------------------------------------------------------------------------


def _company_facts_signals(
    sec_source_pack: Mapping[str, Any] | None,
    profile: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Pull a coarse single-snapshot version of the same shape that
    `_sec_trend_signals` returns, derived from `SEC Source Pack > Company Facts`
    (XBRL latest-value-by-concept). Used as a fallback when the multi-quarter
    SEC Trend Pack is unavailable so we can still produce target bands.

    All values are treated as ANNUAL totals (which is what most companies
    file in their latest 10-K, and what the FACT_SPECS picker tends to
    return). We normalize that to quarterly run-rate so `_compute_targets`
    can use the same annualization (×4) convention.
    """

    out: dict[str, Any] = {
        "status": "company_facts",
        "revenue_yoy": None,
        "accelerating": False,
        "latest_revenue": None,
        "gross_margin": None,
        "opex_run_rate": None,
        "shares_diluted": None,
        "op_leverage": None,
        "segments": None,
    }

    pack = _as_mapping(sec_source_pack)
    facts_raw = pack.get("Company Facts") if pack else None
    facts = _as_mapping(facts_raw)
    if not facts:
        return out

    def _fact_val(name: str) -> float | None:
        node = _as_mapping(facts.get(name))
        if not node:
            return None
        return _as_float(node.get("val"))

    annual_revenue = _fact_val("Revenue")
    annual_op_inc = _fact_val("Operating Income")
    annual_net_inc = _fact_val("Net Income")
    diluted_shares = _fact_val("Shares Outstanding")
    diluted_eps = _fact_val("Diluted EPS")

    # Net Income / Diluted EPS gives implied diluted shares as a fallback.
    if diluted_shares is None and diluted_eps not in (None, 0) and annual_net_inc is not None:
        try:
            diluted_shares = float(annual_net_inc) / float(diluted_eps)
        except (TypeError, ValueError, ZeroDivisionError):
            diluted_shares = None

    if annual_revenue is None or annual_revenue <= 0:
        return out

    # Quarterly run-rate equivalents so the rest of the pipeline can ×4.
    out["latest_revenue"] = annual_revenue / 4.0

    if diluted_shares and diluted_shares > 0:
        out["shares_diluted"] = diluted_shares

    # Build gross margin and opex BELOW the gross profit line so that
    # `_compute_targets` (which does `gross_profit - opex`) produces the
    # right operating-income identity.
    if annual_op_inc is not None:
        op_margin = float(annual_op_inc) / float(annual_revenue)
        # Implied gross margin: assume opex (SG&A + R&D + everything below
        # the gross profit line) is ~20% of revenue when we lack a direct
        # gross-margin reading. That gives gross_margin = op_margin + 0.20.
        gross_margin = max(0.0, min(1.0, op_margin + 0.20))
        out["gross_margin"] = gross_margin
        annual_opex_below_gross = (gross_margin - op_margin) * annual_revenue
        out["opex_run_rate"] = max(0.0, annual_opex_below_gross / 4.0)

    # Try a YoY hint from profile.revenueGrowth (yfinance) if available
    if profile:
        yoy_hint = _as_float(_as_mapping(profile).get("revenueGrowth"))
        if yoy_hint is not None:
            out["revenue_yoy"] = yoy_hint

    return out


def _profile_signals(profile: Mapping[str, Any] | None) -> dict[str, Any]:
    """Final fallback: synthesize the scenario engine's inputs straight from
    the yfinance profile dict. This is the most reliable path on production
    because the SEC EDGAR XBRL endpoint is frequently UA-blocked / rate-
    limited there, while yfinance.Ticker.info almost always returns
    `totalRevenue`, `operatingMargins`, `grossMargins`, `sharesOutstanding`,
    and `revenueGrowth`. All values treated as TTM annual, normalized to
    quarterly run-rate for downstream ×4 consistency.
    """

    out: dict[str, Any] = {
        "status": "profile",
        "revenue_yoy": None,
        "accelerating": False,
        "latest_revenue": None,
        "gross_margin": None,
        "opex_run_rate": None,
        "shares_diluted": None,
        "op_leverage": None,
        "segments": None,
    }

    p = _as_mapping(profile)
    if not p:
        return out

    total_revenue = _as_float(p.get("totalRevenue"))
    gross_margins = _as_float(p.get("grossMargins"))
    operating_margins = _as_float(p.get("operatingMargins"))
    shares_outstanding = _as_float(p.get("sharesOutstanding"))
    revenue_growth = _as_float(p.get("revenueGrowth"))

    if total_revenue is None or total_revenue <= 0:
        return out

    out["latest_revenue"] = total_revenue / 4.0

    # Resolve effective gross margin (preferred) and operating margin so we
    # can derive opex (below the gross-profit line) explicitly. The scenario
    # engine in `_compute_targets` computes:
    #     gross_profit  = revenue * gross_margin
    #     op_income     = gross_profit - opex
    # so opex must be the cost block BELOW gross profit (SG&A + R&D + ...),
    # NOT total cost. Use the gross_margin and operating_margin identity:
    #     opex = (gross_margin - operating_margin) * revenue.
    gm: float | None = None
    if gross_margins is not None and 0 < gross_margins <= 1:
        gm = float(gross_margins)
    elif operating_margins is not None and -1 <= operating_margins <= 1:
        # No direct gross margin — assume opex/revenue ≈ 20%, so
        # gross_margin ≈ op_margin + 0.20.
        gm = max(0.0, min(1.0, float(operating_margins) + 0.20))
    if gm is not None:
        out["gross_margin"] = gm

    if shares_outstanding and shares_outstanding > 0:
        out["shares_diluted"] = shares_outstanding

    if (
        gm is not None
        and operating_margins is not None
        and -1 <= operating_margins <= 1
    ):
        opex_share = max(0.0, gm - float(operating_margins))
        annual_opex_below_gross = opex_share * total_revenue
        out["opex_run_rate"] = annual_opex_below_gross / 4.0

    if revenue_growth is not None:
        out["revenue_yoy"] = revenue_growth

    return out


def _merge_signals(primary: Mapping[str, Any], fallback: Mapping[str, Any]) -> dict[str, Any]:
    """Take any keys missing/None in primary from fallback."""

    out: dict[str, Any] = dict(primary)
    for key, fallback_value in fallback.items():
        if out.get(key) in (None, "", "insufficient"):
            out[key] = fallback_value
    return out


def _compute_targets(
    *,
    sec_signals: Mapping[str, Any],
    history: HistoryResult | None,
    reclassification_gap: float,
) -> tuple[float | None, float | None, float | None, str]:
    revenue = sec_signals.get("latest_revenue")
    gm = sec_signals.get("gross_margin")
    opex = sec_signals.get("opex_run_rate")
    shares = sec_signals.get("shares_diluted")
    yoy = sec_signals.get("revenue_yoy")

    rev_f = _as_float(revenue)
    gm_f = _as_float(gm)
    opex_f = _as_float(opex)
    sh_f = _as_float(shares)
    yoy_f = _as_float(yoy) if yoy is not None else 0.0
    yoy_f = yoy_f if yoy_f is not None else 0.0

    last_price = _last_close(history)

    if rev_f is None or gm_f is None or opex_f is None or sh_f is None or sh_f <= 0:
        return None, None, None, "insufficient data to derive scenarios"

    # Annualize: assume the supplied figures are quarterly run-rate
    # (matches sec_trend latest-quarter convention).
    annual_rev = rev_f * 4
    annual_opex = opex_f * 4

    def _scenario(rev_mult: float, gm_delta: float, multiple: float) -> float | None:
        scenario_rev = annual_rev * rev_mult
        scenario_gm = gm_f + gm_delta
        if scenario_gm <= 0:
            return None
        gross_profit = scenario_rev * scenario_gm
        operating_income = gross_profit - annual_opex
        eps = operating_income / sh_f
        return eps * multiple

    bear_mult = 12.0
    base_mult = 20.0
    bull_mult = 28.0 * (1.0 + reclassification_gap * 0.5)

    bear = _scenario(1.0, -0.03, bear_mult)
    base = _scenario(1.0 + yoy_f, 0.0, base_mult)
    bull = _scenario(1.0 + yoy_f * 1.3, 0.02, bull_mult)

    basis_parts = [
        f"latest-quarter revenue ${rev_f:,.0f} annualized to ${annual_rev:,.0f}",
        f"GM {gm_f * 100:.1f}%, opex run-rate ${opex_f:,.0f}/q",
        f"shares diluted {sh_f:,.0f}",
        f"bear/base/bull multiples {bear_mult:.0f}x / {base_mult:.0f}x / {bull_mult:.1f}x",
    ]
    if last_price is not None:
        basis_parts.append(f"current price ${last_price:.2f}")
    basis = "; ".join(basis_parts)

    def _clean(value: float | None) -> float | None:
        if value is None:
            return None
        # Anything <= 0 is a going-concern / wipeout scenario, not a target
        # price worth displaying. Surface None so the UI shows "—" instead
        # of a negative dollar figure.
        if value <= 0:
            return None
        return round(value, 2)

    return _clean(bear), _clean(base), _clean(bull), basis


# ---------------------------------------------------------------------------
# Catalysts / kill criteria / diligence gaps
# ---------------------------------------------------------------------------


def _catalysts(
    *,
    top_theme: Mapping[str, Any] | None,
    sec_signals: Mapping[str, Any],
    exa_research: Mapping[str, Any] | None,
) -> list[str]:
    theme_label = "infrastructure"
    if top_theme is not None:
        theme_label = str(top_theme.get("verb") or theme_label).split(" ")[0]
        theme_label = theme_label.lower()
    out: list[str] = [
        f"Next quarterly earnings: watch infrastructure/{theme_label} revenue mix",
        "Backlog or order-rate guidance",
    ]

    if exa_research:
        queries = _as_mapping(exa_research.get("Queries"))
        product_bucket = queries.get("product_and_customer")
        if isinstance(product_bucket, list) and product_bucket:
            out.append("Specific named customer ramp referenced in recent disclosures")

    gm = sec_signals.get("gross_margin")
    if isinstance(gm, (int, float)):
        out.append(f"Gross margin stability at >{(float(gm) * 100):.0f}%")
    else:
        out.append("Gross margin stability vs prior quarter")

    out.append("Estimate revisions following next print")

    if top_theme is not None:
        out.append(f"Industry capex confirmation around: {top_theme.get('verb')}")
    else:
        out.append("Sector capex confirmation in customer 10-Qs and call transcripts")

    return out[:6]


def _kill_criteria(
    *,
    top_theme: Mapping[str, Any] | None,
    sec_signals: Mapping[str, Any],
) -> list[str]:
    out: list[str] = [
        "Revenue growth decelerates below 5% YoY for two consecutive quarters",
    ]
    gm = sec_signals.get("gross_margin")
    if isinstance(gm, (int, float)):
        out.append(
            f"Gross margin falls below {(float(gm) * 100) - 5:.0f}%"
        )
    else:
        out.append("Gross margin contracts by more than 5pp")
    out.append("Operating expenses grow faster than revenue for two quarters")
    theme_label = str(top_theme.get("verb")) if top_theme else "thesis"
    out.append(f"{theme_label}-specific: customer cancellations or program delays")
    out.append(
        "Stock fully renamed by sell-side while estimates haven't moved (sell-into-renaming risk)"
    )
    return out[:5]


def _diligence_gaps(
    *,
    sec_source_pack: Mapping[str, Any] | None,
    sec_signals: Mapping[str, Any],
) -> list[str]:
    gaps: list[str] = []
    sections = _as_mapping((sec_source_pack or {}).get("SEC Sections")) or _as_mapping(
        (sec_source_pack or {}).get("Filing Sections")
    )
    expected = ("Business", "MD&A", "Risk Factors")
    missing = [name for name in expected if not _section_text(sections.get(name))]
    if missing:
        gaps.append(
            "SEC items missing from source pack: " + ", ".join(missing)
        )
    gaps.append("Earnings call transcript not pulled (qualitative tone unverified)")
    if not _has_segment_data(sec_signals):
        gaps.append("Segment-level revenue split unavailable")
    gaps.append("Peer comparable analysis not assembled")
    gaps.append("Capacity utilization / lead-time signals not confirmed")
    return gaps


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def score_reclassification(
    *,
    ticker: str,
    profile: dict[str, Any] | None,
    history: HistoryResult | None,
    sec_trend: dict[str, Any] | None,
    sec_source_pack: dict[str, Any] | None,
    exa_research: dict[str, Any] | None,
    torque_result: dict[str, Any] | None = None,
) -> ReclassificationResult:
    """Produce a deterministic reclassification analysis.

    The function is total: it never raises on missing or malformed inputs.
    """

    _ = ticker  # currently informational; kept for screening symmetry
    _ = torque_result  # reserved for future scoring blends

    profile_m = _as_mapping(profile)
    sec_pack_m = _as_mapping(sec_source_pack)
    exa_m: Mapping[str, Any] | None = _as_mapping(exa_research) if exa_research else None
    sec_trend_m = _as_mapping(sec_trend)

    # Treat "not configured" exa as absent for keyword corpus assembly
    exa_for_corpus = exa_m if (exa_m and exa_m.get("Status") != "not configured") else None

    old_noun = _resolve_old_noun(profile_m)
    corpus = _assemble_corpus(profile_m, sec_pack_m, exa_for_corpus)
    ranked = _rank_themes(corpus)
    top_theme: dict[str, Any] | None = ranked[0] if ranked else None
    top_count = int(top_theme.get("evidence_count", 0)) if top_theme else 0

    new_verb_candidates = _public_candidates(ranked)

    # Accept a single strong hit as enough to pick the primary verb, but
    # require >= 2 hits to call it confidently "the" thesis. Below threshold
    # we still record the top candidate so the memo / UI can show it as
    # provisional rather than dropping to the bland default.
    if top_theme is not None and top_count >= 1:
        primary_new_verb = str(top_theme["verb"])
        functional_layer = str(top_theme["layer"])
    else:
        primary_new_verb = DEFAULT_VERB
        functional_layer = DEFAULT_LAYER
        top_theme = None

    hidden_bom_role = _hidden_bom_role(corpus, top_theme, functional_layer)

    sec_signals = _sec_trend_signals(sec_trend_m)
    # Fallback 1: single-snapshot XBRL Company Facts (SEC source pack).
    if sec_signals.get("status") not in ("available",) or sec_signals.get("latest_revenue") in (None, 0):
        sec_signals = _merge_signals(sec_signals, _company_facts_signals(sec_pack_m, profile_m))
    # Fallback 2: yfinance profile (totalRevenue + margins + sharesOutstanding).
    # SEC EDGAR is frequently rate-limited or UA-blocked in production, so the
    # profile path is the one that actually surfaces target bands for most
    # tickers. Only merge in fields the upstream layers didn't already provide.
    if sec_signals.get("latest_revenue") in (None, 0) or sec_signals.get("shares_diluted") in (None, 0):
        sec_signals = _merge_signals(sec_signals, _profile_signals(profile_m))

    gap = _reclassification_gap(
        old_noun=old_noun,
        top_theme=top_theme,
        sec_signals=sec_signals,
        profile=profile_m,
    )
    proof_stage = _proof_stage(
        top_theme=top_theme,
        sec_signals=sec_signals,
        profile=profile_m,
        exa_research=exa_m,
        history=history,
    )
    proof_stage_label = PROOF_STAGE_LABELS.get(proof_stage, PROOF_STAGE_LABELS[0])

    target_low, target_mid, target_high, target_basis = _compute_targets(
        sec_signals=sec_signals,
        history=history,
        reclassification_gap=gap,
    )

    catalysts = _catalysts(
        top_theme=top_theme,
        sec_signals=sec_signals,
        exa_research=exa_m,
    )
    kill_criteria = _kill_criteria(top_theme=top_theme, sec_signals=sec_signals)
    diligence_gaps = _diligence_gaps(
        sec_source_pack=sec_pack_m if sec_source_pack is not None else None,
        sec_signals=sec_signals,
    )

    return ReclassificationResult(
        old_noun=old_noun,
        new_verb_candidates=new_verb_candidates,
        primary_new_verb=primary_new_verb,
        hidden_bom_role=hidden_bom_role,
        functional_layer=functional_layer,
        reclassification_gap=round(gap, 3),
        proof_stage=proof_stage,
        proof_stage_label=proof_stage_label,
        target_low=target_low,
        target_mid=target_mid,
        target_high=target_high,
        target_basis=target_basis,
        catalysts=catalysts,
        kill_criteria=kill_criteria,
        diligence_gaps=diligence_gaps,
    )


__all__ = [
    "ReclassificationResult",
    "STALE_OLD_NOUN_INDUSTRIES",
    "THEME_KEYWORDS",
    "PROOF_STAGE_LABELS",
    "score_reclassification",
]


# Re-export Iterable to satisfy linters that flag the import otherwise.
_ = Iterable
