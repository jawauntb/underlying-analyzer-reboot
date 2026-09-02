"""The Prism memo: a compact packet projection, an LLM pass, and a hard fallback.

The packet is far too large to hand a model whole, so :func:`project_packet`
renders it as a bounded markdown briefing (default 25,000 characters) that keeps
one line per fact and drops nothing structural. The model is asked for JSON so
the recommendation, entry price, exit targets and key determinants come back as
fields rather than as prose a caller has to regex.

Everything the model returns is checked against a recommendation derived
mechanically from the scenario mixture in :func:`derive_recommendation`. That
derivation is also the whole memo when no API key is configured, so the engine
always produces a recommendation with a stated basis - never a blank section and
never an unfounded one.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

DEFAULT_PROJECTION_CHARS = 25_000
DEFAULT_MAX_TOKENS = 8_000

ACTIONS: tuple[str, ...] = ("strong_buy", "buy", "hold", "sell", "strong_sell")
STRENGTHS: tuple[str, ...] = ("strong", "normal", "weak")

DISCLAIMER = "Research only. This is not investment advice and no order was placed."

MEMO_SYSTEM = (
    "You are the analyst behind Prism, a quantitative investment memo engine. You "
    "are given a briefing that contains every number the engine computed, each one "
    "already sourced. Write the memo strictly from that briefing.\n\n"
    "Hard rules:\n"
    "1. Never state a number that is not in the briefing. If a section says it is "
    "unavailable, say the memo could not test it rather than filling the gap.\n"
    "2. Use the recommendation grammar exactly: action is one of strong_buy, buy, "
    "hold, sell, strong_sell; strength is one of strong, normal, weak; conviction is "
    "a number between 0 and 1.\n"
    "3. Distinguish signal from noise: name which inputs are load bearing (the "
    "briefing lists them) and which are decoration.\n"
    "4. Say what is already priced in before saying what is not.\n"
    "5. Cite by citation id in square brackets. Ids are exactly of the form C "
    "followed by a number - [C1], [C7], [C12] - and only ids listed in the "
    "briefing's '## Citations' section exist, each already bound to a specific "
    "claim and source. Never invent an id, never write a named id like "
    "[C_regime], and never cite an id the briefing does not list. Do NOT write "
    "your own citation list and do NOT restate, renumber or re-describe the ids: "
    "the engine appends the canonical list after your memo. An id must mean "
    "exactly what the briefing says it means.\n"
    "6. Never give personalised advice, never promise a return, and end the memo "
    "with the line 'Research only. This is not investment advice and no order was "
    "placed.'\n\n"
    "Reply in exactly this two-block format and nothing else. The fields go in "
    "JSON; the memo itself goes in markdown after it, so a long memo never has to "
    "survive being escaped inside a JSON string:\n\n"
    "<PRISM_JSON>\n"
    '{"action": "...", "strength": "...", "conviction": 0.0, "one_line": "...", '
    '"entry_price": null, "exit_targets": [{"horizon": "6m", "price": 0.0, '
    '"probability": 0.0}], "stop_or_reassess": null, "key_determinants": '
    '[{"name": "...", "explanation": "...", "direction": "bullish|bearish|neutral", '
    '"weight": 0.0}], "priced_in": ["..."], "citation_ids": ["C1"]}\n'
    "</PRISM_JSON>\n"
    "<PRISM_MEMO>\n"
    "# TICKER - Prism memo\n"
    "...markdown...\n"
    "</PRISM_MEMO>\n\n"
    "The markdown memo must have these sections in order: Thesis, Recommendation, "
    "What the numbers say (seasonality, regime, factors, spectral, entropy), "
    "Fundamentals and filings, Macro and cross-asset, Scenarios and levels, "
    "Signal versus noise, What is priced in, Timing, Risks and what would break it. "
    "Do not add a Citations section - the engine appends the canonical one."
)

CITATION_ID = re.compile(r"\[(C\d+)\]")
BRACKET_TOKEN = re.compile(r"\[([A-Za-z][A-Za-z0-9_.-]{0,40})\]")

JSON_BLOCK = re.compile(r"<PRISM_JSON>\s*(\{.*?\})\s*</PRISM_JSON>", re.DOTALL)
MEMO_BLOCK = re.compile(r"<PRISM_MEMO>\s*(.*?)(?:</PRISM_MEMO>|\Z)", re.DOTALL)


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _pct(value: Any, *, digits: int = 2) -> str:
    number = _finite(value)
    return "n/a" if number is None else f"{number * 100:+.{digits}f}%"


def _num(value: Any, *, digits: int = 3) -> str:
    number = _finite(value)
    return "n/a" if number is None else f"{number:,.{digits}f}"


def _money(value: Any) -> str:
    number = _finite(value)
    if number is None:
        return "n/a"
    for cutoff, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(number) >= cutoff:
            return f"${number / cutoff:,.2f}{suffix}"
    return f"${number:,.2f}"


def _section(packet: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    value = packet.get(name)
    return value if isinstance(value, Mapping) else None


def _sub(node: Mapping[str, Any] | None, key: str) -> Mapping[str, Any]:
    """One nested mapping, or an empty one - keeps the projection total."""
    value = (node or {}).get(key)
    return value if isinstance(value, Mapping) else {}


def _section_error(packet: Mapping[str, Any], name: str) -> str | None:
    error = packet.get(f"{name}_error")
    return str(error) if error else None


# --------------------------------------------------------------------------
# Citations
# --------------------------------------------------------------------------


def build_citations(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Numbered, checkable references into the packet and its external sources."""
    citations: list[dict[str, Any]] = []

    def add(claim: str, source: str, url: str | None = None) -> None:
        citations.append(
            {
                "id": f"C{len(citations) + 1}",
                "claim": claim,
                "source": source,
                "url": url,
            }
        )

    ticker = str(packet.get("ticker") or "")
    seasonality = _section(packet, "seasonality")
    if seasonality:
        month = seasonality.get("month_label") or "this month"
        subject = seasonality.get("ticker")
        if isinstance(subject, Mapping):
            window = (subject.get("this_month") or {}).get("10y") or {}
            add(
                f"{ticker} {month} mean return over the last {window.get('n', 0)} years: "
                f"{_pct((window or {}).get('mean'))}",
                "prism.seasonality.ticker.this_month.10y",
            )
    regimes = _section(packet, "regimes")
    if regimes:
        current = regimes.get("current") or {}
        add(
            f"Market regime is '{current.get('label')}' with switch confidence "
            f"{_num(current.get('switch_confidence'))}",
            "prism.regimes.current",
        )
    factors = _section(packet, "factors")
    if factors:
        windows = factors.get("windows") or {}
        one_year = windows.get("1y") if isinstance(windows, Mapping) else None
        if isinstance(one_year, Mapping):
            add(
                f"One-year factor alpha {_pct(one_year.get('alpha_annual'))} annualised, "
                f"R^2 {_num(one_year.get('r2'))}",
                f"prism.factors.windows.1y ({factors.get('model')})",
            )
    entropy = _section(packet, "entropy")
    if entropy:
        windows = entropy.get("windows") or {}
        three = windows.get("3m") if isinstance(windows, Mapping) else None
        if isinstance(three, Mapping):
            add(
                f"Three-month return entropy {_num(three.get('H'))} "
                f"({three.get('classification')} on the fixed +/-3 sigma grid)",
                "prism.entropy.windows.3m",
            )
    spectral = _section(packet, "spectral")
    if spectral:
        modes = spectral.get("modes") or []
        if modes:
            top = modes[0]
            add(
                f"Dominant cycle {_num(top.get('period_days'), digits=1)} days, "
                f"currently {top.get('cycle_position')}",
                "prism.spectral.modes[0]",
            )
    fundamentals = _section(packet, "fundamentals")
    if fundamentals:
        growth = fundamentals.get("growth") or {}
        add(
            f"Revenue {_pct(growth.get('revenue_yoy'))} year over year; stage "
            f"'{(fundamentals.get('stage') or {}).get('label')}'",
            f"prism.fundamentals ({fundamentals.get('provider')})",
        )
        ratios = fundamentals.get("ratios") or {}
        add(
            f"Trailing P/E {_num(ratios.get('pe'), digits=2)}, "
            f"EV/EBITDA {_num(ratios.get('ev_ebitda'), digits=2)}",
            "prism.fundamentals.ratios",
        )
    filings = _section(packet, "filings")
    if filings:
        for row in list(filings.get("ten_k") or [])[:2] + list(filings.get("ten_q") or [])[:3]:
            if not isinstance(row, Mapping):
                continue
            add(
                f"{row.get('form')} filed {row.get('filing_date')} for the period ending "
                f"{row.get('report_date')}",
                "SEC EDGAR",
                str(row.get("url") or "") or None,
            )
    macro = _section(packet, "macro")
    if macro:
        vix = macro.get("vix") or {}
        add(
            f"VIX {_num(vix.get('current'), digits=2)} as of {vix.get('as_of')}",
            "FRED VIXCLS",
            "https://fred.stlouisfed.org/series/VIXCLS",
        )
        curve = macro.get("curve_shape") or {}
        add(
            f"Yield curve {curve.get('label')} (2s10s {_num(curve.get('2s10s'), digits=2)})",
            "FRED DGS2/DGS10/T10Y2Y",
            "https://fred.stlouisfed.org/series/T10Y2Y",
        )
    volatility = _section(packet, "volatility")
    if volatility:
        realized = (volatility.get("realized") or {}).get("3m") or {}
        implied = volatility.get("implied") or {}
        add(
            f"Three-month realized volatility {_pct(realized.get('annualized'))} annualised; "
            f"ATM implied {_pct(implied.get('atm_iv'))}",
            "prism.volatility",
        )
    news = _section(packet, "news")
    if news:
        for item in list(news.get("items") or [])[:6]:
            if not isinstance(item, Mapping):
                continue
            add(
                f"{item.get('category')}: {item.get('title')}",
                str(item.get("source") or item.get("provider") or "news"),
                str(item.get("url") or "") or None,
            )
    scenarios = _section(packet, "scenarios")
    if scenarios:
        cases = scenarios.get("cases") or {}
        bull = cases.get("bull") if isinstance(cases, Mapping) else None
        if isinstance(bull, Mapping):
            add(
                f"Bull case probability {_num(bull.get('probability'), digits=3)} at the "
                f"{scenarios.get('probability_horizon')} horizon",
                "prism.scenarios.cases",
            )
    levels = _section(packet, "levels")
    if levels:
        auction = levels.get("auction") or {}
        add(
            f"Auction value area {_num(auction.get('val'), digits=2)} to "
            f"{_num(auction.get('vah'), digits=2)}, point of control "
            f"{_num(auction.get('poc'), digits=2)}",
            "prism.levels.auction",
        )
    return citations


# --------------------------------------------------------------------------
# Deterministic recommendation
# --------------------------------------------------------------------------


def derive_recommendation(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Mechanically derive the recommendation from the scenario mixture.

    The score is the case-probability edge (bull minus bear at the reference
    horizon) plus the discount to fair value, each capped so neither can run away
    with the answer. Conviction starts from the score's magnitude and is cut when
    the return distribution is classified as noise or when sections are missing —
    a memo built on three of eleven sections should not sound certain.
    """
    scenarios = _section(packet, "scenarios") or {}
    cases = _sub(scenarios, "cases")
    entry = _sub(scenarios, "entry")
    horizon = str(scenarios.get("probability_horizon") or "3m")

    bull = _finite((cases.get("bull") or {}).get("probability")) or 0.0
    bear = _finite((cases.get("bear") or {}).get("probability")) or 0.0
    edge = bull - bear

    current = _finite(entry.get("current_price")) or _finite(scenarios.get("current_price"))
    fair = _finite(entry.get("fair_value"))
    value_gap: float | None = None
    if current and fair and current > 0:
        value_gap = fair / current - 1.0

    score = edge + (max(-0.5, min(0.5, value_gap)) if value_gap is not None else 0.0)

    if score > 0.35:
        action = "strong_buy"
    elif score > 0.12:
        action = "buy"
    elif score > -0.12:
        action = "hold"
    elif score > -0.35:
        action = "sell"
    else:
        action = "strong_sell"

    conviction = min(1.0, abs(score) / 0.6)
    entropy = _section(packet, "entropy") or {}
    three_month = _sub(_sub(entropy, "windows"), "3m")
    classification = str(three_month.get("classification") or "")
    if classification == "noise":
        conviction *= 0.7
    elif classification == "structure":
        conviction = min(1.0, conviction * 1.15)

    populated = sum(
        1
        for name in (
            "seasonality",
            "macro",
            "relational",
            "factors",
            "regimes",
            "entropy",
            "spectral",
            "eigen",
            "fundamentals",
            "filings",
            "volatility",
            "levels",
            "news",
            "scenarios",
        )
        if packet.get(name) is not None
    )
    coverage = populated / 14.0
    conviction *= 0.5 + 0.5 * coverage
    conviction = round(max(0.0, min(1.0, conviction)), 3)

    strength = "strong" if conviction >= 0.66 else ("normal" if conviction >= 0.33 else "weak")

    ticker = str(packet.get("ticker") or "the ticker")
    one_line = (
        f"{ticker}: {action.replace('_', ' ')} ({strength}) - the {horizon} mixture puts "
        f"{bull:.0%} on the bull case against {bear:.0%} on the bear case"
    )
    if value_gap is not None:
        one_line += f", with fair value {value_gap:+.1%} from the last price"
    one_line += "."

    return {
        "action": action,
        "strength": strength,
        "conviction": conviction,
        "one_line": one_line,
        "score": round(score, 4),
        "basis": {
            "probability_edge": round(edge, 4),
            "value_gap": None if value_gap is None else round(value_gap, 4),
            "horizon": horizon,
            "entropy_classification": classification or None,
            "section_coverage": round(coverage, 3),
        },
    }


def derive_targets(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Entry, exit targets and the reassessment price, from the mixture cases."""
    scenarios = _section(packet, "scenarios") or {}
    cases = _sub(scenarios, "cases")
    entry_block = _sub(scenarios, "entry")
    bull = _sub(cases, "bull")
    bear = _sub(cases, "bear")

    exit_targets: list[dict[str, Any]] = []
    bull_horizons = _sub(bull, "horizons")
    if isinstance(bull_horizons, Mapping):
        for horizon in ("3m", "6m", "12m"):
            block = bull_horizons.get(horizon)
            if not isinstance(block, Mapping):
                continue
            price = _finite(block.get("price_p50"))
            if price is None:
                continue
            exit_targets.append(
                {
                    "horizon": horizon,
                    "price": price,
                    "probability": _finite(block.get("probability")),
                    "basis": "bull-case median price at this horizon",
                }
            )

    stop: float | None = None
    bear_horizons = _sub(bear, "horizons")
    if isinstance(bear_horizons, Mapping):
        block = bear_horizons.get("3m") if isinstance(bear_horizons.get("3m"), Mapping) else None
        if isinstance(block, Mapping):
            stop = _finite(block.get("price_p50"))

    return {
        "entry_price": _finite(entry_block.get("bargain_below")),
        "fair_value": _finite(entry_block.get("fair_value")),
        "expensive_above": _finite(entry_block.get("expensive_above")),
        "current_price": _finite(entry_block.get("current_price")),
        "exit_targets": exit_targets,
        "stop_or_reassess": stop,
    }


def clean_exit_targets(value: Any, fallback: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep only model-returned exit targets that match the published contract.

    ``PrismExitTarget`` in ``packages/core`` requires an object with a string
    ``horizon``; a plausible model reply such as ``["3m: 313"]`` used to be
    passed through untouched and made the *entire* ``PrismPacket`` fail to parse
    for every client that honours the schema. Anything unusable is dropped, and
    when nothing survives the engine-derived targets stand.
    """
    rows: list[dict[str, Any]] = []
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for item in value:
            if not isinstance(item, Mapping):
                continue
            horizon = item.get("horizon")
            if horizon is None or not str(horizon).strip():
                continue
            row: dict[str, Any] = {
                "horizon": str(horizon).strip(),
                "price": _finite(item.get("price")),
                "probability": _finite(item.get("probability")),
            }
            basis = item.get("basis")
            if basis is not None:
                row["basis"] = str(basis)
            rows.append(row)
    if rows:
        return rows
    return [dict(row) for row in fallback if isinstance(row, Mapping)]


def clean_key_determinants(
    value: Any, fallback: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Keep only model-returned determinants that match the published contract.

    ``PrismKeyDeterminant`` requires object entries with string ``name`` and
    ``explanation``; bare strings break the whole packet's schema parse.
    """
    rows: list[dict[str, Any]] = []
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for item in value:
            if not isinstance(item, Mapping):
                continue
            name = item.get("name")
            explanation = item.get("explanation")
            if name is None or not str(name).strip():
                continue
            if explanation is None or not str(explanation).strip():
                continue
            row: dict[str, Any] = {
                "name": str(name).strip(),
                "explanation": str(explanation).strip(),
            }
            direction = item.get("direction")
            row["direction"] = (
                str(direction).strip().lower()
                if str(direction or "").strip().lower()
                in {"bullish", "bearish", "neutral", "unknown"}
                else "unknown"
            )
            row["weight"] = _finite(item.get("weight"))
            rows.append(row)
    if rows:
        return rows
    return [dict(row) for row in fallback if isinstance(row, Mapping)]


def clean_priced_in(value: Any, fallback: Sequence[Any]) -> list[str]:
    """``priced_in`` is a list of strings in the contract; coerce or fall back."""
    items = value if isinstance(value, Sequence) and not isinstance(value, str | bytes) else []
    rows: list[str] = []
    for item in items:
        text = str(item.get("claim") or item.get("name") or "") if isinstance(item, Mapping) \
            else str(item or "")
        text = text.strip()
        if text:
            rows.append(text)
    if rows:
        return rows
    return [str(item) for item in fallback]


def key_determinants(packet: Mapping[str, Any], *, limit: int = 6) -> list[dict[str, Any]]:
    """What actually moved the answer: scenario weights crossed with load-bearing."""
    scenarios = _section(packet, "scenarios") or {}
    weights = _sub(scenarios, "weights")
    components = _sub(scenarios, "components")
    eigen = _section(packet, "eigen") or {}
    load_bearing = {
        str(row.get("signal")): row
        for row in (eigen.get("load_bearing") or [])
        if isinstance(row, Mapping)
    }

    rows: list[dict[str, Any]] = []
    for name, weight in sorted(
        ((str(key), _finite(value) or 0.0) for key, value in (weights or {}).items()),
        key=lambda item: -item[1],
    )[: max(1, int(limit))]:
        component = components.get(name) if isinstance(components, Mapping) else None
        expected = None
        if isinstance(component, Mapping):
            expected_map = component.get("expected_return")
            if isinstance(expected_map, Mapping):
                expected = _finite(expected_map.get("6m"))
        marker = load_bearing.get(name)
        rows.append(
            {
                "name": name,
                "explanation": (
                    str((component or {}).get("basis") or "")
                    or f"{name} component of the scenario mixture"
                ),
                "direction": (
                    "unknown" if expected is None else ("bullish" if expected > 0 else "bearish")
                ),
                "weight": round(weight, 4),
                "expected_return_6m": expected,
                "available": bool((component or {}).get("available")),
                "load_bearing": bool((marker or {}).get("load_bearing")),
                "weight_delta_if_removed": _finite((marker or {}).get("weight_delta_if_removed")),
            }
        )
    return rows


def priced_in(packet: Mapping[str, Any]) -> list[str]:
    """What the market already reflects, taken from the packet, never invented."""
    out: list[str] = []
    scenarios = _section(packet, "scenarios") or {}
    entry = _sub(scenarios, "entry")
    gap = _finite(entry.get("current_vs_fair"))
    if gap is not None:
        out.append(
            f"The last price sits {gap:+.1%} against the mixture's six-month fair value, "
            "so that much of the modelled outcome is already in the price."
        )
    volatility = _section(packet, "volatility") or {}
    premium = _finite(volatility.get("variance_risk_premium"))
    if premium is not None:
        out.append(
            f"Options are charging {premium:+.1%} over trailing one-month realized "
            "volatility, which is the market's own price for the uncertainty ahead."
        )
    factors = _section(packet, "factors") or {}
    residuals = _sub(factors, "residuals")
    residual = _finite(residuals.get("last_60d_cum"))
    if residual is not None:
        out.append(
            f"Factor-adjusted, the last sixty sessions carry {residual:+.1%} of residual "
            "return - that part is not explained by market, size, value, profitability, "
            "investment or momentum exposure."
        )
    fundamentals = _section(packet, "fundamentals") or {}
    ratios = _sub(fundamentals, "ratios")
    pe = _finite(ratios.get("pe"))
    forecast = _sub(fundamentals, "forecast")
    implied = _finite(forecast.get("implied_revenue_growth"))
    if pe is not None and implied is not None:
        out.append(
            f"At {pe:,.1f}x trailing earnings the market is paying ahead of the "
            f"{implied:+.1%} revenue growth the trend-plus-seasonal forecast carries."
        )
    return out


# --------------------------------------------------------------------------
# Packet projection
# --------------------------------------------------------------------------


def _truncate_block(lines: Sequence[str], budget: int, *, label: str) -> list[str]:
    """Keep whole lines of ``lines`` until ``budget`` characters are used up."""
    kept: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + 1
        if used + cost > budget:
            kept.append(f"- [{label} truncated to fit the briefing budget]")
            kept.append("")
            break
        kept.append(line)
        used += cost
    return kept


def project_packet(packet: Mapping[str, Any], *, max_chars: int = DEFAULT_PROJECTION_CHARS) -> str:
    """Render the packet as a bounded markdown briefing for a language model.

    The briefing is **budgeted per section**, not truncated at the end. A large-cap
    packet's filings and news blocks alone run past 20k characters, and the old
    tail-truncation silently dropped the citation catalogue, the scenario mixture
    and the honest gaps list — the three blocks the memo is required to be written
    from. Those now form a reserved tail; ``filings`` and ``news`` are the elastic
    middle and absorb the shortfall.
    """
    ticker = str(packet.get("ticker") or "")
    head: list[str] = [
        f"# Prism briefing: {ticker}",
        f"as_of={packet.get('as_of')} generated_at={packet.get('generated_at')} "
        f"engine={packet.get('engine_version')}",
        "",
    ]

    profile = _section(packet, "profile")
    if profile:
        head += [
            "## Profile",
            f"- name: {profile.get('name')}",
            f"- sector: {profile.get('sector')} | industry: {profile.get('industry')}",
            f"- market cap: {_money(profile.get('market_cap'))} | exchange: "
            f"{profile.get('primary_exchange')} | listed since: {profile.get('listed_since')}",
            f"- related ETFs: {', '.join(profile.get('related_etfs') or []) or 'n/a'}",
            f"- description: {str(profile.get('description') or '')[:600]}",
            "",
        ]

    head += _project_seasonality(packet)
    head += _project_macro(packet)
    head += _project_relational(packet)
    head += _project_factors(packet)
    head += _project_regimes(packet)
    head += _project_entropy_spectral(packet)
    head += _project_eigen(packet)
    head += _project_fundamentals(packet)
    head += _project_volatility_levels(packet)
    head += _project_recent(packet)

    # Reserved tail: the model cannot write a cited, scenario-grounded memo
    # without these, so they are never the block that gets dropped.
    tail: list[str] = (
        _project_scenarios(packet) + _project_gaps(packet) + _project_citations(packet)
    )

    def size(block: Sequence[str]) -> int:
        return sum(len(line) + 1 for line in block)

    head_size, tail_size = size(head), size(tail)
    # Margin for the "[... truncated]" markers `_truncate_block` may append.
    remaining = max_chars - head_size - tail_size - 256
    filings = _project_filings(packet)
    news = _project_news(packet)
    elastic_size = size(filings) + size(news)

    if remaining >= elastic_size:
        body = filings + news
    elif remaining <= 0:
        # Even head+tail overruns: keep the tail whole and trim the head instead,
        # so the citations and scenarios still reach the model.
        body = []
        head = _truncate_block(head, max(max_chars - tail_size - 256, 0), label="briefing body")
    else:
        share = remaining / float(elastic_size)
        body = _truncate_block(
            filings, int(size(filings) * share), label="filings"
        ) + _truncate_block(news, int(size(news) * share), label="news")

    text = "\n".join(head + body + tail)
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 80].rstrip()}\n\n[briefing truncated at {max_chars} characters]"


def _project_seasonality(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "seasonality")
    if not section:
        return ["## Seasonality", f"- unavailable: {_section_error(packet, 'seasonality')}", ""]
    lines = ["## Seasonality", f"- calendar month: {section.get('month_label')}"]
    subject = section.get("ticker")
    if isinstance(subject, Mapping):
        lines.append(f"- {packet.get('ticker')} this month:")
        for window, block in (subject.get("this_month") or {}).items():
            if not isinstance(block, Mapping):
                continue
            lines.append(
                f"  - {window}: mean {_pct(block.get('mean'))} median {_pct(block.get('median'))} "
                f"hit {_num(block.get('hit_rate'), digits=2)} n={block.get('n')}"
            )
        trend = subject.get("trend") or {}
        lines.append(
            f"  - trend: {trend.get('direction')} (slope {_num(trend.get('slope'), digits=5)})"
        )
        for horizon, block in (subject.get("forward") or {}).items():
            if not isinstance(block, Mapping):
                continue
            lines.append(
                f"  - forward {horizon}: mean {_pct(block.get('mean'))} "
                f"p10 {_pct(block.get('p10'))} p90 {_pct(block.get('p90'))} "
                f"hit {_num(block.get('hit_rate'), digits=2)} n={block.get('n')}"
            )
    benchmarks = section.get("benchmarks")
    if isinstance(benchmarks, Mapping):
        for symbol, stats in list(benchmarks.items())[:8]:
            if not isinstance(stats, Mapping):
                continue
            block = (stats.get("this_month") or {}).get("10y") or {}
            lines.append(
                f"- {symbol} this month (10y): mean {_pct(block.get('mean'))} "
                f"hit {_num(block.get('hit_rate'), digits=2)} n={block.get('n')}"
            )
    lines.append("")
    return lines


def _project_macro(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "macro")
    if not section:
        return ["## Macro", f"- unavailable: {_section_error(packet, 'macro')}", ""]
    lines = ["## Macro"]
    yields = section.get("yields")
    if isinstance(yields, Mapping):
        for series_id, block in yields.items():
            if not isinstance(block, Mapping):
                continue
            lines.append(
                f"- {series_id}: {_num(block.get('current'), digits=2)} "
                f"(1m {_num(block.get('change_1m'), digits=2)}, "
                f"3m {_num(block.get('change_3m'), digits=2)}, "
                f"12m {_num(block.get('change_12m'), digits=2)})"
            )
    curve = section.get("curve_shape") or {}
    lines.append(
        f"- curve: {curve.get('label')} 2s10s {_num(curve.get('2s10s'), digits=2)} "
        f"5s20s {_num(curve.get('5s20s'), digits=2)}"
    )
    for key in ("vix", "hy_spread", "dollar", "wti", "brent", "gold", "btc", "nfp"):
        block = section.get(key)
        if not isinstance(block, Mapping):
            continue
        lines.append(
            f"- {key}: {_num(block.get('current'), digits=2)} as of {block.get('as_of')} "
            f"(1m {_num(block.get('change_1m'), digits=3)}, "
            f"3m {_num(block.get('change_3m'), digits=3)}, "
            f"12m {_num(block.get('change_12m'), digits=3)})"
        )
    fx = section.get("fx")
    if isinstance(fx, Mapping):
        pairs = ", ".join(
            f"{name} {_num((block or {}).get('current'), digits=3)}"
            for name, block in fx.items()
            if isinstance(block, Mapping)
        )
        lines.append(f"- fx: {pairs}")
    lines.append("")
    return lines


def _project_relational(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "relational")
    if not section:
        return ["## Cross-asset", f"- unavailable: {_section_error(packet, 'relational')}", ""]
    lines = ["## Cross-asset (gauge-fixed)", f"- frame: {section.get('reference_frame')}"]
    beta = section.get("beta")
    if isinstance(beta, Mapping):
        for symbol, block in list(beta.items())[:10]:
            if not isinstance(block, Mapping):
                continue
            lines.append(
                f"- beta {symbol}: 3m {_num(block.get('3m'), digits=2)} "
                f"1y {_num(block.get('1y'), digits=2)} "
                f"rolling63 {_num(block.get('current_rolling_63d'), digits=2)} "
                f"({block.get('rolling_trend')})"
            )
    correlation = section.get("correlation")
    if isinstance(correlation, Mapping):
        for symbol, block in list(correlation.items())[:10]:
            if not isinstance(block, Mapping):
                continue
            lines.append(
                f"- corr {symbol}: 3m {_num(block.get('3m'), digits=2)} "
                f"1y {_num(block.get('1y'), digits=2)}"
            )
    kinematics = section.get("kinematics")
    if isinstance(kinematics, Mapping):
        for symbol, block in list(kinematics.items())[:6]:
            if not isinstance(block, Mapping):
                continue
            lines.append(
                f"- kinematics {symbol}: velocity {_num(block.get('velocity'), digits=5)} "
                f"acceleration {_num(block.get('acceleration'), digits=5)} "
                f"jerk {_num(block.get('jerk'), digits=5)}"
            )
    impact = section.get("impact_weights")
    if isinstance(impact, Mapping):
        ranked = sorted(
            (
                (str(symbol), _finite((block or {}).get("weight")) or 0.0)
                for symbol, block in impact.items()
                if isinstance(block, Mapping)
            ),
            key=lambda item: -abs(item[1]),
        )[:8]
        lines.append(
            "- impact weights: "
            + ", ".join(f"{symbol} {weight:+.3f}" for symbol, weight in ranked)
        )
    rma = section.get("relative_moving_average")
    if isinstance(rma, Mapping):
        lines.append(
            f"- relative moving average: {_num(rma.get('value'), digits=4)} "
            f"({rma.get('signal')})"
        )
    lines.append("")
    return lines


def _project_factors(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "factors")
    if not section:
        return ["## Factors", f"- unavailable: {_section_error(packet, 'factors')}", ""]
    lines = ["## Factors", f"- model: {section.get('model')}"]
    stale_days = _finite(section.get("stale_days"))
    if section.get("as_of"):
        lines.append(
            f"- factor data as of {section.get('as_of')}"
            + (
                f" — {int(stale_days)} days behind the price series"
                if stale_days is not None and stale_days > 0
                else ""
            )
        )
    if stale_days is not None and stale_days > 7:
        # Ken French publishes with a lag. Without this the model reads a
        # two-month-old residual as "recent" and calls it what NVDA is doing now.
        lines.append(
            f"- STALENESS RULE: every factor window and the residual below end on "
            f"{section.get('as_of')}, {int(stale_days)} days before this packet's as-of. "
            "Describe them by that date. Never call them 'recent', 'lately' or "
            "'currently'."
        )
    windows = section.get("windows")
    if isinstance(windows, Mapping):
        for window, block in windows.items():
            if not isinstance(block, Mapping):
                continue
            betas = block.get("betas") or {}
            exposure = ", ".join(
                f"{name} {_num(value, digits=2)}" for name, value in betas.items()
            )
            lines.append(
                f"- {window} ({block.get('start')}..{block.get('end')}): alpha "
                f"{_pct(block.get('alpha_annual'))} annualised, "
                f"R^2 {_num(block.get('r2'), digits=3)}, residual vol "
                f"{_pct(block.get('residual_vol_annual'))}, n={block.get('n')} | {exposure}"
            )
    premia = section.get("premia")
    if isinstance(premia, Mapping) and isinstance(premia.get("daily"), Mapping):
        priced = ", ".join(
            f"{name} {_pct(value * 252.0, digits=2)}/yr"
            for name, value in premia["daily"].items()
            if _finite(value) is not None
        )
        lines.append(
            f"- premia used to price the exposures ({premia.get('source')}, "
            f"{premia.get('start')}..{premia.get('end')}): {priced}"
        )
    residuals = section.get("residuals")
    if isinstance(residuals, Mapping):
        lines.append(
            f"- residual over the 20/60 sessions ending {residuals.get('as_of')}: "
            f"20d {_pct(residuals.get('last_20d_cum'))}, "
            f"60d {_pct(residuals.get('last_60d_cum'))}, "
            f"z {_num(residuals.get('z_score'), digits=2)}"
        )
    lines.append("")
    return lines


def _project_regimes(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "regimes")
    if not section:
        return ["## Regimes", f"- unavailable: {_section_error(packet, 'regimes')}", ""]
    lines = [
        "## Regimes (3-state Gaussian HMM on daily return and 10-day volatility)",
        f"- trained on {section.get('trained_on')} over {section.get('train_window_days')} days",
    ]
    for state in section.get("states") or []:
        if not isinstance(state, Mapping):
            continue
        lines.append(
            f"- state {state.get('id')} '{state.get('label')}': mean daily "
            f"{_pct(state.get('mean_daily_return'), digits=3)}, vol "
            f"{_num(state.get('volatility'), digits=4)}, occupancy "
            f"{_num(state.get('occupancy'), digits=3)}, avg duration "
            f"{_num(state.get('avg_duration_days'), digits=1)}d"
        )
    current = section.get("current") or {}
    lines.append(
        f"- current: {current.get('label')} for {current.get('days_in_regime')} days, "
        f"switch confidence {_num(current.get('switch_confidence'), digits=3)}, "
        f"posterior {[round(float(value), 3) for value in (current.get('posterior') or [])]}"
    )
    by_regime = section.get("ticker_by_regime")
    if isinstance(by_regime, Mapping):
        for label, block in by_regime.items():
            if not isinstance(block, Mapping):
                continue
            lines.append(
                f"- {packet.get('ticker')} in {label}: mean daily "
                f"{_pct(block.get('mean_daily'), digits=3)}, sharpe "
                f"{_num(block.get('sharpe'), digits=2)}, hit "
                f"{_num(block.get('hit_rate'), digits=2)}, n={block.get('n')}"
            )
    lines.append("")
    return lines


def _project_entropy_spectral(packet: Mapping[str, Any]) -> list[str]:
    lines: list[str] = ["## Entropy"]
    entropy = _section(packet, "entropy")
    if not entropy:
        lines.append(f"- unavailable: {_section_error(packet, 'entropy')}")
    else:
        lines.append(
            f"- grid: {entropy.get('bin_grid')} - {entropy.get('bins')} equal-width bins over "
            f"+/-{_num(entropy.get('sigma_multiple'), digits=1)} full-sample sigma "
            f"(sigma {_num(entropy.get('sigma_full_sample'), digits=4)}), H normalised by "
            "log2(bins). 'structure' = the window concentrates in a few cells of that fixed "
            "grid, 'noise' = it fills them; a dispersion reading, not a forecast."
        )
        for window, block in (entropy.get("windows") or {}).items():
            if not isinstance(block, Mapping):
                continue
            lines.append(
                f"- {window}: H {_num(block.get('H'), digits=3)} "
                f"({block.get('classification')}) n={block.get('n')}"
            )
        backtest = entropy.get("backtest") or {}
        lines.append(
            "- backtest: low-entropy win rate "
            f"{_num(backtest.get('low_entropy_win_rate'), digits=3)} "
            f"(n={backtest.get('n_low')}) vs high-entropy "
            f"{_num(backtest.get('high_entropy_win_rate'), digits=3)} "
            f"(n={backtest.get('n_high')}), edge {_num(backtest.get('edge'), digits=3)}"
        )
    lines.append("")

    lines.append("## Spectral")
    spectral = _section(packet, "spectral")
    if not spectral:
        lines.append(f"- unavailable: {_section_error(packet, 'spectral')}")
    else:
        lines.append(f"- reconstruction R^2: {_num(spectral.get('reconstruction_r2'), digits=3)}")
        for mode in (spectral.get("modes") or [])[:5]:
            if not isinstance(mode, Mapping):
                continue
            lines.append(
                f"- mode {_num(mode.get('period_days'), digits=1)}d: power share "
                f"{_num(mode.get('power_share'), digits=3)}, position "
                f"{mode.get('cycle_position')} at phase "
                f"{_num(mode.get('phase_fraction'), digits=3)}"
            )
        projection = spectral.get("projection")
        if isinstance(projection, Mapping):
            for horizon, block in projection.items():
                if not isinstance(block, Mapping):
                    continue
                lines.append(
                    f"- projection {horizon}: {_pct(block.get('expected_return'))} "
                    f"(confidence {_num(block.get('confidence'), digits=2)})"
                )
        consistency = spectral.get("consistency") or {}
        lines.append(
            f"- consistency: {consistency.get('likelihood_label')} "
            f"(z {_num(consistency.get('z'), digits=2)})"
        )
    lines.append("")
    return lines


def _project_eigen(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "eigen")
    if not section:
        return [
            "## Eigen / signal ranking",
            f"- unavailable: {_section_error(packet, 'eigen')}",
            "",
        ]
    lines = ["## Eigen / signal ranking"]
    pca = section.get("pca") or {}
    ratios = pca.get("explained_variance_ratio") or []
    if ratios:
        lines.append(
            "- PCA explained variance: "
            + ", ".join(f"{float(value):.3f}" for value in ratios[:5])
        )
    for row in (section.get("signal_ranking") or [])[:10]:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            f"- {row.get('signal')}: corr 1y {_num(row.get('corr_1y'), digits=3)}, "
            f"6m {_num(row.get('corr_6m'), digits=3)}, 3m {_num(row.get('corr_3m'), digits=3)} "
            f"(rank {row.get('rank')})"
        )
    symmetry = section.get("symmetry") or {}
    broken = symmetry.get("broken_pairs") or []
    invariant = symmetry.get("gauge_invariant_pairs") or []
    lines.append(f"- symmetry: {len(broken)} broken pairs, {len(invariant)} gauge-invariant pairs")
    for row in (section.get("load_bearing") or [])[:10]:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            f"- {row.get('signal')}: dropping it changes the other signals' weights by "
            f"{_num(row.get('survivor_weight_delta'), digits=4)} (raw delta "
            f"{_num(row.get('weight_delta_if_removed'), digits=4)}, own weight "
            f"{_num(row.get('baseline_weight'), digits=4)}) -> "
            f"{'LOAD BEARING' if row.get('load_bearing') else 'decoration'}"
        )
    lines.append("")
    return lines


def _project_fundamentals(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "fundamentals")
    if not section:
        return ["## Fundamentals", f"- unavailable: {_section_error(packet, 'fundamentals')}", ""]
    lines = ["## Fundamentals", f"- provider: {section.get('provider')}"]
    for row in (section.get("quarters") or [])[:8]:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            f"- {row.get('period_end')} (FY{row.get('fiscal_year')} Q{row.get('fiscal_quarter')}): "
            f"revenue {_money(row.get('revenue'))}, gross {_money(row.get('gross_profit'))}, "
            f"operating {_money(row.get('operating_income'))}, "
            f"net {_money(row.get('net_income'))}, "
            f"eps {_num(row.get('eps'), digits=2)}, fcf {_money(row.get('fcf'))}, "
            f"gm {_pct(row.get('gross_margin'), digits=1)}, "
            f"om {_pct(row.get('operating_margin'), digits=1)}"
        )
    ratios = section.get("ratios") or {}
    lines.append(
        f"- ratios: P/E {_num(ratios.get('pe'), digits=2)}, "
        f"P/S {_num(ratios.get('ps'), digits=2)}, "
        f"P/B {_num(ratios.get('pb'), digits=2)}, "
        f"EV/EBITDA {_num(ratios.get('ev_ebitda'), digits=2)}, "
        f"EV/EBIT {_num(ratios.get('ev_ebit'), digits=2)}, "
        f"EV/Sales {_num(ratios.get('ev_sales'), digits=2)}, "
        f"D/E {_num(ratios.get('debt_to_equity'), digits=2)}, "
        f"FCF yield {_pct(ratios.get('fcf_yield'))}, "
        f"dividend yield {_pct(ratios.get('dividend_yield'))}, NAV/share "
        f"{_num(ratios.get('nav_per_share'), digits=2)}"
    )
    growth = section.get("growth") or {}
    lines.append(
        f"- growth: revenue yoy {_pct(growth.get('revenue_yoy'))}, qoq "
        f"{_pct(growth.get('revenue_qoq'))}, net income yoy {_pct(growth.get('net_income_yoy'))}, "
        f"margins {growth.get('margin_trend')}, acceleration "
        f"{_pct(growth.get('revenue_growth_acceleration'))}"
    )
    forecast = section.get("forecast") or {}
    if forecast.get("next_4q"):
        lines.append(
            f"- forecast ({forecast.get('method')}): next-twelve-month revenue "
            f"{_money(forecast.get('forward_revenue_ntm'))}, implied growth "
            f"{_pct(forecast.get('implied_revenue_growth'))}"
        )
    stage = section.get("stage") or {}
    lines.append(f"- stage: {stage.get('label')} - {'; '.join(stage.get('evidence') or [])}")
    lines.append("")
    return lines


def _project_filings(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "filings")
    if not section:
        return ["## Filings", f"- unavailable: {_section_error(packet, 'filings')}", ""]
    lines = ["## Filings (SEC EDGAR)"]
    for row in list(section.get("ten_k") or []) + list(section.get("ten_q") or []):
        if not isinstance(row, Mapping):
            continue
        lines.append(
            f"- {row.get('form')} filed {row.get('filing_date')} "
            f"(period {row.get('report_date')}) {row.get('url')}"
        )
        summary = row.get("summary")
        if summary:
            lines.append(f"  - summary: {str(summary)[:1200]}")
    synthesis = section.get("synthesis") or {}
    for key in (
        "performance",
        "risks",
        "growth_opportunities",
        "new_business_lines",
        "operating_context",
        "capex_suppliers_customers",
    ):
        value = synthesis.get(key)
        if value:
            lines.append(f"- {key.replace('_', ' ')}: {str(value)[:1000]}")
    lines.append("")
    return lines


def _project_volatility_levels(packet: Mapping[str, Any]) -> list[str]:
    lines = ["## Volatility"]
    volatility = _section(packet, "volatility")
    if not volatility:
        lines.append(f"- unavailable: {_section_error(packet, 'volatility')}")
    else:
        for window, block in (volatility.get("realized") or {}).items():
            if not isinstance(block, Mapping):
                continue
            lines.append(
                f"- realized {window}: {_pct(block.get('annualized'))} annualised, "
                f"avg {_pct(block.get('avg'))}, percentile "
                f"{_num(block.get('percentile'), digits=2)}"
            )
        lines.append(f"- vol of vol: {_pct(volatility.get('vol_of_vol'))}")
        implied = volatility.get("implied") or {}
        lines.append(
            f"- implied: ATM {_pct(implied.get('atm_iv'))} at {implied.get('expiry')} "
            f"({implied.get('expiry_kind')}), 25d skew {_pct(implied.get('skew_25d'))}"
        )
        regime_avg = volatility.get("regime_avg") or {}
        for label, block in regime_avg.items():
            if isinstance(block, Mapping):
                lines.append(
                    f"- vol in {label} regime: {_pct(block.get('avg_annualized'))} "
                    f"(n={block.get('n_days')})"
                )
    lines.append("")

    lines.append("## Levels")
    levels = _section(packet, "levels")
    if not levels:
        lines.append(f"- unavailable: {_section_error(packet, 'levels')}")
    else:
        auction = levels.get("auction") or {}
        lines.append(
            f"- auction: VAH {_num(auction.get('vah'), digits=2)}, POC "
            f"{_num(auction.get('poc'), digits=2)}, VAL {_num(auction.get('val'), digits=2)} "
            f"({auction.get('location')}, {auction.get('window')})"
        )
        regression = levels.get("regression") or {}
        lines.append(
            f"- regression: trend {_num(regression.get('trend_value'), digits=2)}, band "
            f"{_num(regression.get('lower_band'), digits=2)}-"
            f"{_num(regression.get('upper_band'), digits=2)}, z "
            f"{_num(regression.get('z_from_trend'), digits=2)}, "
            f"EMA21 {_num(regression.get('ema21'), digits=2)} "
            f"EMA50 {_num(regression.get('ema50'), digits=2)} "
            f"EMA200 {_num(regression.get('ema200'), digits=2)}"
        )
        torque = levels.get("torque") or {}
        lines.append(
            f"- torque: score {_num(torque.get('total_score'), digits=1)} "
            f"stage {torque.get('stage_label')} ({torque.get('recommendation')})"
        )
        for row in (levels.get("key_levels") or [])[:10]:
            if isinstance(row, Mapping):
                lines.append(
                    f"- key level {_num(row.get('price'), digits=2)} ({row.get('kind')}, "
                    f"{row.get('source')}, {_pct(row.get('distance_pct'), digits=1)} away)"
                )
    lines.append("")
    return lines


def _project_news(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "news")
    if not section:
        return ["## News", f"- unavailable: {_section_error(packet, 'news')}", ""]
    lines = ["## News and policy"]
    for item in (section.get("items") or [])[:18]:
        if not isinstance(item, Mapping):
            continue
        lines.append(
            f"- [{item.get('category')}] {item.get('title')} ({item.get('source')}, "
            f"{item.get('published')}) {item.get('url')}"
        )
        summary = item.get("summary")
        if summary:
            lines.append(f"  - {str(summary)[:320]}")
    lines.append("")
    return lines


def _project_scenarios(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "scenarios")
    if not section:
        return ["## Scenarios", f"- unavailable: {_section_error(packet, 'scenarios')}", ""]
    lines = ["## Scenarios"]
    weights = section.get("weights") or {}
    lines.append(
        "- weights: "
        + ", ".join(f"{name} {_num(value, digits=3)}" for name, value in weights.items())
    )
    evidence = section.get("weight_evidence")
    if isinstance(evidence, Mapping):
        lines.append(f"- weight evidence: {json.dumps(evidence, default=str)[:900]}")
    prior = section.get("prior")
    if isinstance(prior, Mapping) and prior.get("by_horizon"):
        lines.append(
            f"- calibration: every component is shrunk toward the market prior "
            f"({prior.get('source')}, {_pct(prior.get('annualized_drift'))} a year) by "
            "1 - confidence, then clipped to the ticker's own [p5, p95] of rolling "
            "horizon returns; raw and shrunk values are in scenarios.components[*].shrinkage"
        )
        moves = []
        for name, component in (section.get("components") or {}).items():
            shrinkage = component.get("shrinkage") if isinstance(component, Mapping) else None
            if not isinstance(shrinkage, Mapping) or not shrinkage.get("applied"):
                continue
            raw = (shrinkage.get("raw_expected_return") or {}).get("12m")
            final = (shrinkage.get("expected_return") or {}).get("12m")
            if raw is None or final is None:
                continue
            moves.append(f"{name} {_pct(raw)} -> {_pct(final)}")
        if moves:
            lines.append("- calibration 12m: " + ", ".join(moves))
    for case, block in (section.get("cases") or {}).items():
        if not isinstance(block, Mapping):
            continue
        lines.append(
            f"- {case}: probability {_num(block.get('probability'), digits=3)} - "
            f"{block.get('narrative')}"
        )
        for horizon, horizon_block in (block.get("horizons") or {}).items():
            if not isinstance(horizon_block, Mapping):
                continue
            lines.append(
                f"  - {horizon}: p10 {_pct(horizon_block.get('p10'))} "
                f"p50 {_pct(horizon_block.get('p50'))} p90 {_pct(horizon_block.get('p90'))} "
                f"| price {_num(horizon_block.get('price_p10'), digits=2)}/"
                f"{_num(horizon_block.get('price_p50'), digits=2)}/"
                f"{_num(horizon_block.get('price_p90'), digits=2)}"
            )
    for horizon, block in (section.get("distribution") or {}).items():
        if isinstance(block, Mapping):
            lines.append(
                f"- distribution {horizon}: mean {_pct(block.get('mean'))} sd "
                f"{_num(block.get('std'), digits=4)} skew {_num(block.get('skew'), digits=2)} "
                f"kurtosis {_num(block.get('kurtosis'), digits=2)}"
            )
    entry = section.get("entry") or {}
    lines.append(
        f"- entry: bargain below {_num(entry.get('bargain_below'), digits=2)}, fair "
        f"{_num(entry.get('fair_value'), digits=2)}, expensive above "
        f"{_num(entry.get('expensive_above'), digits=2)}, current "
        f"{_num(entry.get('current_price'), digits=2)} "
        f"({_pct(entry.get('current_vs_fair'))} vs fair)"
    )
    timing = section.get("timing") or {}
    lines.append(f"- timing this month: {timing.get('this_month')} - {timing.get('reason')}")
    for signal in (section.get("watch_signals") or [])[:8]:
        if isinstance(signal, Mapping):
            lines.append(
                f"- watch {signal.get('symbol')}: {signal.get('condition')} -> "
                f"{signal.get('implication')}"
            )
    lines.append("")
    return lines


def _project_recent(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "recent")
    if not section:
        return []
    lines = ["## Recent"]
    for window, block in section.items():
        if not isinstance(block, Mapping):
            continue
        lines.append(
            f"- {window}: return {_pct(block.get('return'))}, vs SPY "
            f"{_pct(block.get('vs_spy'))}, vs sector {_pct(block.get('vs_sector'))}, "
            f"vol {_pct(block.get('volatility'))}, entropy "
            f"{_num(block.get('entropy'), digits=3)}, regime {block.get('regime')}"
        )
        if block.get("notable"):
            lines.append(f"  - notable: {block.get('notable')}")
    lines.append("")
    return lines


def _project_gaps(packet: Mapping[str, Any]) -> list[str]:
    meta = packet.get("meta")
    if not isinstance(meta, Mapping):
        return []
    lines = ["## What the engine could NOT compute"]
    for row in meta.get("errors") or []:
        if isinstance(row, Mapping):
            lines.append(f"- {row.get('source')}: {row.get('error')}")
    for row in meta.get("unavailable") or []:
        if isinstance(row, Mapping):
            lines.append(f"- unavailable {row.get('source')}: {row.get('reason')}")
    if len(lines) == 1:
        lines.append("- nothing; every section built")
    lines.append("")
    return lines


#: A trailing ``## Citations`` / ``## References`` / ``## Sources`` heading and
#: everything after it. The model used to be *asked* for its own citation list,
#: so it invented its own numbering and every [Cn] in the prose resolved to a
#: different claim than the same id in ``memo.citations``.
MODEL_CITATION_BLOCK = re.compile(
    r"\n#{1,6}\s*(?:citations?|references?|sources?)\s*:?\s*\n.*\Z",
    re.IGNORECASE | re.DOTALL,
)


def strip_model_citations(text: str) -> tuple[str, str | None]:
    """Remove a model-authored trailing citation list.

    Returns ``(text_without_it, removed_block_or_None)`` so the caller can both
    append the canonical list and check the model's own glosses against it.
    """
    match = MODEL_CITATION_BLOCK.search(text or "")
    if not match:
        return (text or "").rstrip(), None
    return (text[: match.start()]).rstrip(), match.group(0).strip()


def render_citations(citations: Sequence[Mapping[str, Any]]) -> str:
    """The canonical citation list, exactly as the engine numbered it."""
    lines = ["## Citations"]
    for citation in citations:
        url = citation.get("url")
        lines.append(
            f"- [{citation.get('id')}] {citation.get('claim')} "
            f"(source: {citation.get('source')}" + (f", {url}" if url else "") + ")"
        )
    return "\n".join(lines)


def citation_glosses(block: str | None) -> dict[str, str]:
    """``{"C7": "Regimes (HMM states...)"}`` parsed out of a model citation list."""
    if not block:
        return {}
    out: dict[str, str] = {}
    for line in block.splitlines():
        match = re.match(r"\s*[-*\d.)\s]*\[?(C\d+)\]?\s*[:.–—-]?\s*(.+)", line.strip())
        if match:
            out.setdefault(match.group(1), match.group(2).strip())
    return out


def mismatched_citation_ids(
    glosses: Mapping[str, str], citations: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Ids whose model-written gloss shares no word with the canonical entry.

    A crude but decisive check: if the model wrote "[C7] Regimes (HMM states)"
    while the engine's C7 is "Trailing P/E 27.50, EV/EBITDA 25.92", the two have
    no meaningful token in common and every [C7] in the prose is mis-resolved.
    """
    catalogue = {str(row.get("id")): row for row in citations}
    stop = {
        "the", "a", "an", "of", "and", "or", "in", "on", "at", "to", "for", "is",
        "over", "with", "by", "from", "its", "per", "last", "current", "prism",
    }

    def tokens(text: Any) -> set[str]:
        return {
            word
            for word in re.findall(r"[a-z]{3,}", str(text).lower())
            if word not in stop
        }

    bad: list[str] = []
    for identifier, gloss in glosses.items():
        entry = catalogue.get(identifier)
        if entry is None:
            continue
        canonical = tokens(entry.get("claim")) | tokens(entry.get("source"))
        written = tokens(gloss)
        if written and canonical and not (written & canonical):
            bad.append(identifier)
    return sorted(bad, key=lambda value: int(value[1:]))


def _project_citations(packet: Mapping[str, Any]) -> list[str]:
    citations = build_citations(packet)
    lines = ["## Citations you may reference by id"]
    for citation in citations:
        lines.append(
            f"- [{citation['id']}] {citation['claim']} (source: {citation['source']}"
            + (f", {citation['url']}" if citation.get("url") else "")
            + ")"
        )
    lines.append("")
    return lines


# --------------------------------------------------------------------------
# Memo assembly
# --------------------------------------------------------------------------


def _parse_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", candidate, flags=re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            return None
        candidate = candidate[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_memo_reply(raw: str) -> dict[str, Any] | None:
    """Read the two-block reply, falling back to a single JSON object.

    The markdown memo is delivered outside the JSON on purpose: a two-thousand
    word document escaped into a JSON string is one unbalanced quote away from
    unparseable, and a reply truncated at the token limit loses everything. With
    the memo in its own block a truncated reply still yields the fields and
    however much of the memo arrived.
    """
    if not raw:
        return None
    json_match = JSON_BLOCK.search(raw)
    memo_match = MEMO_BLOCK.search(raw)
    if json_match:
        try:
            parsed = json.loads(json_match.group(1))
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            if memo_match:
                body = memo_match.group(1).strip()
                if body:
                    parsed["text"] = body
                    parsed["truncated"] = "</PRISM_MEMO>" not in raw
            return parsed
    legacy = _parse_json_object(raw)
    if isinstance(legacy, dict):
        return legacy
    if memo_match and memo_match.group(1).strip():
        # Fields did not survive, but the prose did: keep the memo and let the
        # engine-derived recommendation supply every field.
        return {"text": memo_match.group(1).strip(), "truncated": "</PRISM_MEMO>" not in raw}
    return None


def fallback_memo(packet: Mapping[str, Any], *, reason: str) -> dict[str, Any]:
    """A complete, honest memo assembled from the packet with no model call."""
    recommendation = derive_recommendation(packet)
    targets = derive_targets(packet)
    determinants = key_determinants(packet)
    citations = build_citations(packet)
    ticker = str(packet.get("ticker") or "")
    text = render_markdown(
        packet,
        recommendation=recommendation,
        targets=targets,
        determinants=determinants,
        priced=priced_in(packet),
        citations=citations,
    )
    return {
        "recommendation": {
            "action": recommendation["action"],
            "strength": recommendation["strength"],
            "conviction": recommendation["conviction"],
            "one_line": recommendation["one_line"],
        },
        "derivation": recommendation,
        "entry_price": targets["entry_price"],
        "exit_targets": targets["exit_targets"],
        "stop_or_reassess": targets["stop_or_reassess"],
        "fair_value": targets["fair_value"],
        "text": text,
        "key_determinants": determinants,
        "priced_in": priced_in(packet),
        "citations": citations,
        "model": None,
        "method": "deterministic",
        "reason": reason,
        "ticker": ticker,
        "generated_at": datetime.now(UTC).isoformat(),
    }


def render_markdown(
    packet: Mapping[str, Any],
    *,
    recommendation: Mapping[str, Any],
    targets: Mapping[str, Any],
    determinants: Sequence[Mapping[str, Any]],
    priced: Sequence[str],
    citations: Sequence[Mapping[str, Any]],
) -> str:
    """Deterministic markdown memo, used verbatim when no model is available."""
    ticker = str(packet.get("ticker") or "")
    profile = _section(packet, "profile") or {}
    scenarios = _section(packet, "scenarios") or {}
    timing = _sub(scenarios, "timing")
    seasonality = _section(packet, "seasonality") or {}
    regimes = _section(packet, "regimes") or {}
    entropy = _section(packet, "entropy") or {}
    fundamentals = _section(packet, "fundamentals") or {}
    filings = _section(packet, "filings") or {}
    synthesis = _sub(filings, "synthesis")

    lines: list[str] = [
        f"# {ticker} - Prism memo",
        "",
        f"*{profile.get('name') or ticker} | {profile.get('sector') or 'sector n/a'} / "
        f"{profile.get('industry') or 'industry n/a'} | as of {packet.get('as_of')}*",
        "",
        "## Recommendation",
        "",
        f"**{recommendation['action'].replace('_', ' ').upper()}** "
        f"({recommendation['strength']}, conviction {recommendation['conviction']:.2f})",
        "",
        recommendation["one_line"],
        "",
        f"- Entry (bargain below): {_num(targets.get('entry_price'), digits=2)}",
        f"- Fair value: {_num(targets.get('fair_value'), digits=2)}",
        f"- Reassess below: {_num(targets.get('stop_or_reassess'), digits=2)}",
    ]
    for target in targets.get("exit_targets") or []:
        lines.append(
            f"- Exit target {target.get('horizon')}: {_num(target.get('price'), digits=2)} "
            f"(bull-case probability {_num(target.get('probability'), digits=2)})"
        )

    lines += ["", "## What the numbers say", ""]
    subject = _sub(seasonality, "ticker")
    window = _sub(_sub(subject, "this_month"), "10y")
    lines.append(
        f"- Seasonality: {seasonality.get('month_label')} has averaged "
        f"{_pct(window.get('mean'))} over {window.get('n', 0)} observed years "
        f"(hit rate {_num(window.get('hit_rate'), digits=2)})."
    )
    current_regime = _sub(regimes, "current")
    lines.append(
        f"- Regime: the market has been in the '{current_regime.get('label')}' state for "
        f"{current_regime.get('days_in_regime')} days, switch confidence "
        f"{_num(current_regime.get('switch_confidence'), digits=2)}."
    )
    three = _sub(_sub(entropy, "windows"), "3m")
    spread = (
        "concentrate in a few cells of"
        if str(three.get("classification")) == "structure"
        else "spread across"
    )
    lines.append(
        f"- Entropy: three-month return entropy is {_num(three.get('H'), digits=3)} "
        f"({three.get('classification')}) on the fixed +/-3 sigma grid - the window's "
        f"returns {spread} the ticker's long-run range, which sets how much weight any "
        "single signal deserves right now."
    )
    stage = _sub(fundamentals, "stage")
    growth = _sub(fundamentals, "growth")
    lines.append(
        f"- Fundamentals: revenue {_pct(growth.get('revenue_yoy'))} year over year, "
        f"margins {growth.get('margin_trend')}, stage '{stage.get('label')}'."
    )
    if synthesis.get("performance"):
        lines += ["", "## Filings", "", str(synthesis["performance"])]
        if synthesis.get("risks"):
            lines += ["", str(synthesis["risks"])]

    lines += ["", "## Signal versus noise", ""]
    for row in determinants:
        marker = "load bearing" if row.get("load_bearing") else "supporting"
        lines.append(
            f"- **{row.get('name')}** (weight {_num(row.get('weight'), digits=3)}, {marker}): "
            f"{row.get('explanation')} - direction {row.get('direction')}."
        )

    lines += ["", "## What is priced in", ""]
    if priced:
        lines += [f"- {item}" for item in priced]
    else:
        lines.append("- The packet did not produce a valuation gap to call priced in.")

    lines += [
        "",
        "## Timing",
        "",
        f"- This month reads **{timing.get('this_month')}**: {timing.get('reason')}",
    ]
    for signal in (scenarios.get("watch_signals") or [])[:5]:
        if isinstance(signal, Mapping):
            lines.append(
                f"- Watch {signal.get('symbol')}: {signal.get('condition')} would mean "
                f"{signal.get('implication')}."
            )

    lines += ["", "## Risks and what would break this", ""]
    meta = packet.get("meta")
    errors = (meta or {}).get("errors") if isinstance(meta, Mapping) else []
    if errors:
        lines.append(
            "- The following inputs failed and are not represented in the recommendation: "
            + "; ".join(
                f"{row.get('source')} ({row.get('error')})"
                for row in errors
                if isinstance(row, Mapping)
            )[:800]
        )
    lines.append(
        f"- A move below {_num(targets.get('stop_or_reassess'), digits=2)} puts price in the "
        "bear case's central path and the thesis should be re-run rather than defended."
    )

    lines += ["", "## Citations", ""]
    for citation in citations:
        lines.append(
            f"- [{citation['id']}] {citation['claim']} - {citation['source']}"
            + (f" ({citation['url']})" if citation.get("url") else "")
        )

    lines += ["", DISCLAIMER, ""]
    return "\n".join(lines)


def build_memo(
    packet: Mapping[str, Any],
    *,
    text_generator: Any | None = None,
    api_key: str | None = None,
    text_model: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    projection_chars: int = DEFAULT_PROJECTION_CHARS,
    temperature: float = 0.2,
) -> dict[str, Any]:
    """Build ``packet["memo"]``.

    Uses ``text_generator`` when supplied, otherwise builds an Anthropic client
    from ``api_key``. With no key at all the deterministic memo is returned with
    ``method="deterministic"`` and the reason recorded - never an empty section.
    """
    generator = text_generator
    if generator is None:
        if not api_key:
            return fallback_memo(packet, reason="no text generator and no ANTHROPIC_API_KEY")
        from app.anthropic import AnthropicTextClient

        generator = AnthropicTextClient(api_key=api_key, model=text_model)

    derived = derive_recommendation(packet)
    targets = derive_targets(packet)
    determinants = key_determinants(packet)
    citations = build_citations(packet)
    citation_ids = {citation["id"] for citation in citations}

    briefing = project_packet(packet, max_chars=projection_chars)
    prompt = (
        f"{briefing}\n\n"
        "## Engine-derived baseline (you may disagree, but say why in the memo)\n"
        f"- derived action: {derived['action']} ({derived['strength']}), conviction "
        f"{derived['conviction']:.2f}\n"
        f"- derivation basis: {json.dumps(derived['basis'], default=str)}\n"
        f"- entry {targets['entry_price']}, fair {targets['fair_value']}, "
        f"reassess {targets['stop_or_reassess']}\n"
        f"- exit targets: {json.dumps(targets['exit_targets'], default=str)}\n"
        f"- weighted components: {json.dumps(determinants, default=str)[:2000]}\n\n"
        "Write the memo now. Return strict JSON only."
    )

    try:
        generated = generator.generate_text(
            system=MEMO_SYSTEM,
            prompt=prompt,
            max_tokens=int(max_tokens),
            temperature=float(temperature),
        )
    except Exception as exc:  # noqa: BLE001 - a model outage still yields a memo
        return fallback_memo(packet, reason=f"text generation failed: {exc}")

    raw = str(getattr(generated, "text", "") or "")
    parsed = parse_memo_reply(raw)
    model = getattr(generated, "model", None)
    if not parsed or not str(parsed.get("text") or "").strip():
        memo = fallback_memo(packet, reason="model did not return a parseable memo object")
        memo["model"] = model
        memo["model_raw_excerpt"] = raw[:2000]
        memo["model_raw_chars"] = len(raw)
        return memo

    action = str(parsed.get("action") or "").strip().lower()
    if action not in ACTIONS:
        action = derived["action"]
    strength = str(parsed.get("strength") or "").strip().lower()
    if strength not in STRENGTHS:
        strength = derived["strength"]
    conviction = _finite(parsed.get("conviction"))
    conviction = derived["conviction"] if conviction is None else max(0.0, min(1.0, conviction))

    text = str(parsed.get("text") or "").strip()
    # The model is told not to write its own citation list; if it does anyway, the
    # list is stripped and checked against the canonical catalogue before the
    # engine's own list is appended. A mismatched id means every [Cn] in the prose
    # points at the wrong claim, which is worse than no memo at all.
    text, model_block = strip_model_citations(text)
    glosses = citation_glosses(model_block)
    mismatched = mismatched_citation_ids(glosses, citations)
    if mismatched:
        memo = fallback_memo(
            packet,
            reason=(
                "citation ids did not resolve: the model renumbered "
                f"{', '.join(mismatched)} against the engine catalogue"
            ),
        )
        memo["model"] = model
        memo["mismatched_citation_ids"] = mismatched
        return memo

    declared = {str(value) for value in (parsed.get("citation_ids") or [])}
    in_text = set(CITATION_ID.findall(text))
    used_ids = sorted(
        (declared | in_text) & citation_ids, key=lambda value: int(value[1:])
    )
    # Anything else in square brackets that looks like a citation is the model
    # inventing an id. Say so rather than letting an unverifiable "[C_regime]"
    # read like a checked reference.
    unknown_ids = sorted(
        {token for token in BRACKET_TOKEN.findall(text) if token not in citation_ids}
    )
    # The canonical citation list goes in before the closing disclaimer, which
    # must remain the memo's last line.
    body = text.rstrip()
    if body.endswith(DISCLAIMER):
        body = body[: -len(DISCLAIMER)].rstrip()
    if citations:
        body = f"{body}\n\n{render_citations(citations)}"
    text = f"{body}\n\n{DISCLAIMER}"

    return {
        "recommendation": {
            "action": action,
            "strength": strength,
            "conviction": round(conviction, 3),
            "one_line": str(parsed.get("one_line") or derived["one_line"]),
        },
        "derivation": derived,
        "entry_price": _finite(parsed.get("entry_price")) or targets["entry_price"],
        "exit_targets": clean_exit_targets(parsed.get("exit_targets"), targets["exit_targets"]),
        "stop_or_reassess": (
            _finite(parsed.get("stop_or_reassess")) or targets["stop_or_reassess"]
        ),
        "fair_value": targets["fair_value"],
        "text": text,
        "key_determinants": clean_key_determinants(
            parsed.get("key_determinants"), determinants
        ),
        "priced_in": clean_priced_in(parsed.get("priced_in"), priced_in(packet)),
        "citations": citations,
        "citation_ids_used": used_ids,
        "unknown_citation_ids": unknown_ids,
        "model": model,
        "method": "model",
        "reason": None,
        "ticker": str(packet.get("ticker") or ""),
        "generated_at": datetime.now(UTC).isoformat(),
        "projection_chars": len(briefing),
        "model_output_chars": len(raw),
        "truncated": bool(parsed.get("truncated")),
    }
