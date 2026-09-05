"""The Situate memo (SPEC §6).

Situate never says "buy" or "sell" and never prints a point price target. It
reports a **posture** — ``odds_favorable`` / ``balanced`` / ``odds_unfavorable``
at a stated horizon — beside the distribution the odds block carries, with a base
rate next to every conditional number and the option-implied view alongside the
historical one. Every quantitative claim cites the module and version that
produced it, and the memo always ends with the research-only disclaimer.

The template is fixed (SPEC §6): headline, what you're buying, state, odds by
horizon, the business, scenarios, zones, three falsifiers, confidence + caveats,
appendix. A language model may rewrite the prose from the same structured
briefing, but with no API key the deterministic template stands on its own — it
is never an empty section.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

# The citation-text utilities are pure string helpers; reuse Prism's rather than
# forking them (the packet shape they operate on is identical: {id, claim,
# source, url}). This keeps the "model must not renumber citations" guard in one
# place across both engines.
from app.prism.memo import (
    citation_glosses,
    mismatched_citation_ids,
    parse_memo_reply,
    render_citations,
    strip_model_citations,
)
from app.situate.contract import ENGINE_NAME, ENGINE_VERSION, HORIZONS

MEMO_VERSION = "1.0.0"
DISCLAIMER = (
    "Research only. This is not investment advice, contains no price target and "
    "no buy or sell recommendation, and no order was placed."
)

DEFAULT_MAX_TOKENS = 4_000
DEFAULT_PROJECTION_CHARS = 22_000

#: The horizon the headline posture is struck at, preferred first.
POSTURE_HORIZON_PREFERENCE: tuple[int, ...] = (6, 3, 12, 2, 1, 18)

POSTURES: tuple[str, ...] = ("odds_favorable", "balanced", "odds_unfavorable")

MEMO_SYSTEM = (
    "You are Situate, writing one research memo that SITUATES a stock: what it is "
    "exposed to, what the odds look like per horizon, what options are pricing, and "
    "what the business is saying. You are NOT a price forecaster.\n\n"
    "Hard rules:\n"
    "1. Never write 'buy', 'sell', 'strong buy', a price target, or a single "
    "expected price. Report a POSTURE (odds_favorable / balanced / odds_unfavorable) "
    "at a stated horizon, and distributions (quantiles), never points.\n"
    "2. Put the unconditional base rate beside every conditional number, and the "
    "option-implied view beside the historical one; call out where they disagree.\n"
    "3. Use 'the data suggests', not 'this stock will'. Attribute uncertainty.\n"
    "4. Cite every quantitative claim by its citation id in square brackets, e.g. "
    "[C3], using ONLY the ids listed in the briefing's '## Citations' section. Do "
    "not write your own citation list and do not renumber an id.\n"
    "5. Keep the fixed section order: Headline; What you're buying; State; Odds by "
    "horizon; The business; Scenarios; Zones; What would prove this wrong; "
    "Confidence and caveats; Appendix.\n\n"
    "Return STRICT JSON with keys: posture (one of odds_favorable|balanced|"
    "odds_unfavorable), horizon (integer months), conviction (0..1), one_line, "
    "text (the full markdown memo), falsifiers (array of exactly 3 strings), "
    "citation_ids (array of the ids you used)."
)


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _pct(value: Any, *, digits: int = 1, sign: bool = False) -> str:
    number = _finite(value)
    if number is None:
        return "n/a"
    formatted = f"{number * 100:{'+' if sign else ''}.{digits}f}%"
    return formatted


def _num(value: Any, *, digits: int = 2) -> str:
    number = _finite(value)
    return f"{number:.{digits}f}" if number is not None else "n/a"


def _money(value: Any) -> str:
    number = _finite(value)
    if number is None:
        return "n/a"
    return f"${number:,.2f}"


def _section(packet: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = packet.get(name)
    return value if isinstance(value, Mapping) else {}


def _mget(node: Any, key: str) -> dict[str, Any]:
    """The child mapping at ``key`` as a plain dict (or ``{}``), for terse access."""
    value = node.get(key) if isinstance(node, Mapping) else None
    return dict(value) if isinstance(value, Mapping) else {}


def _module_version(packet: Mapping[str, Any], module: str) -> str:
    meta = _section(packet, "meta")
    versions = meta.get("versions") if isinstance(meta.get("versions"), Mapping) else {}
    if isinstance(versions, Mapping) and versions.get(module):
        return str(versions[module])
    section = packet.get(module)
    if isinstance(section, Mapping) and section.get("version"):
        return str(section["version"])
    return "1.0.0"


# --------------------------------------------------------------------------
# Odds / posture
# --------------------------------------------------------------------------


def _odds_by_horizon(packet: Mapping[str, Any]) -> dict[str, Any]:
    return _mget(_section(packet, "odds"), "by_horizon")


def choose_posture_horizon(packet: Mapping[str, Any]) -> int | None:
    """The first preferred horizon that has a usable odds distribution."""
    by_h = _odds_by_horizon(packet)
    for h in POSTURE_HORIZON_PREFERENCE:
        block = by_h.get(str(h))
        if isinstance(block, Mapping) and isinstance(block.get("quantiles"), Mapping):
            return h
    for h in HORIZONS:
        block = by_h.get(str(h))
        if isinstance(block, Mapping) and isinstance(block.get("quantiles"), Mapping):
            return h
    return None


def derive_posture(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Posture + conviction from the odds distribution (no buy/sell grammar).

    ``odds_favorable`` when the odds lean up (P(up) high and the median positive),
    ``odds_unfavorable`` when they lean down, ``balanced`` otherwise. Conviction
    scales the tilt by how much data stood behind it (the shrink weight) — a thin
    sample can never produce a confident posture.
    """
    horizon = choose_posture_horizon(packet)
    ticker = str(packet.get("ticker") or "this name")
    if horizon is None:
        return {
            "stance": "balanced",
            "horizon": None,
            "conviction": 0.0,
            "p_up": None,
            "q50": None,
            "one_line": (
                f"The data suggests no usable forward-return distribution for {ticker}; "
                "posture is balanced by default."
            ),
        }
    block = _mget(_odds_by_horizon(packet), str(horizon))
    quantiles = _mget(block, "quantiles")
    p_up = _finite(block.get("p_up"))
    q50 = _finite(quantiles.get("q50"))
    shrink_w = _finite(block.get("shrink_w"))

    stance = "balanced"
    if p_up is not None and q50 is not None:
        if p_up >= 0.58 and q50 > 0:
            stance = "odds_favorable"
        elif p_up <= 0.42 and q50 < 0:
            stance = "odds_unfavorable"

    tilt = abs(p_up - 0.5) * 2.0 if p_up is not None else 0.0
    # A thin conditional sample (low shrink weight) caps conviction: with w near 0
    # the conditional collapsed to the base rate and we know little that is
    # name-specific.
    data_factor = 0.4 + 0.6 * (shrink_w if shrink_w is not None else 0.0)
    conviction = round(max(0.0, min(1.0, tilt * data_factor)), 3)

    one_line = (
        f"The data suggests {stance.replace('_', ' ')} odds for {ticker} at "
        f"{horizon} months: P(up) {_pct(p_up, digits=0)} with a median move of "
        f"{_pct(q50, sign=True)} and a shrink weight of {_num(shrink_w)}."
    )
    return {
        "stance": stance,
        "horizon": horizon,
        "conviction": conviction,
        "p_up": p_up,
        "q50": q50,
        "shrink_w": shrink_w,
        "one_line": one_line,
    }


# --------------------------------------------------------------------------
# Citations — every quantitative claim binds to module + version
# --------------------------------------------------------------------------


def build_citations(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Bind each quantitative claim in the memo to its module and version."""
    citations: list[dict[str, Any]] = []

    def add(module: str, claim: str, *, url: str | None = None) -> None:
        version = _module_version(packet, module)
        cid = f"C{len(citations) + 1}"
        citations.append(
            {
                "id": cid,
                "claim": claim,
                "module": module,
                "version": version,
                "source": f"{module} v{version}",
                "url": url,
            }
        )

    exposure = _section(packet, "exposure")
    if exposure:
        betas = exposure.get("betas") if isinstance(exposure.get("betas"), Mapping) else {}
        spy_beta = _finite((betas or {}).get("SPY"))
        add(
            "exposure",
            f"SPY beta {_num(spy_beta)}, R² {_num(exposure.get('r2'))}, "
            f"idiosyncratic share {_pct(exposure.get('idiosyncratic_share'))}",
        )
        factor = exposure.get("factor") if isinstance(exposure.get("factor"), Mapping) else {}
        if factor and isinstance(factor.get("loadings"), Mapping):
            add("exposure", f"Fama-French MKT loading {_num((factor['loadings']).get('MKT'))}")

    state = _section(packet, "state")
    if state:
        spy_cell = _mget(state, "spy").get("cell")
        tk_cell = _mget(state, "ticker").get("cell")
        add("state", f"SPY state cell '{spy_cell}', ticker state cell '{tk_cell}'")

    base = _section(packet, "base_rates")
    if base:
        by_h = _mget(base, "by_horizon")
        for h in (3, 12):
            block = _mget(by_h, str(h))
            shrunk = _mget(block, "shrunk")
            if shrunk:
                add(
                    "base_rates",
                    f"{h}-month shrunk conditional median {_pct(shrunk.get('q50'), sign=True)} "
                    f"(n_eff {_num(shrunk.get('n_eff'), digits=1)}, w {_num(shrunk.get('w'))})",
                )

    implied = _section(packet, "implied")
    if implied:
        by_h = _mget(implied, "by_horizon")
        for h in HORIZONS:
            block = _mget(by_h, str(h))
            if block.get("iv_atm") is not None:
                add(
                    "implied",
                    f"{h}-month ATM IV {_pct(block.get('iv_atm'))}, 25-delta skew "
                    f"{_num(block.get('skew_25d'), digits=3)}, width ratio "
                    f"{_num(block.get('width_ratio_vs_hist'))}",
                )
                break

    odds = _section(packet, "odds")
    if odds:
        posture_h = choose_posture_horizon(packet)
        if posture_h is not None:
            block = _odds_by_horizon(packet).get(str(posture_h)) or {}
            add(
                "odds",
                f"{posture_h}-month P(up) {_pct(block.get('p_up'), digits=0)} from "
                f"source '{block.get('source')}'",
            )

    fundamentals = _section(packet, "fundamentals")
    if fundamentals:
        quality = _mget(fundamentals, "quality")
        add(
            "fundamentals",
            f"gross profit/assets {_num(quality.get('gp_to_assets'))}, "
            f"net debt/EBITDA {_num(quality.get('net_debt_ebitda'))}",
        )

    text = _section(packet, "text")
    if text:
        changes = text.get("filing_changes") or []
        if changes and isinstance(changes[0], Mapping):
            add(
                "text",
                f"filing material-change score "
                f"{_num(changes[0].get('material_change_score'), digits=1)} "
                f"in {changes[0].get('section') or 'the latest filing'}",
            )

    levels = _section(packet, "levels")
    if levels and (levels.get("cheap_zone") or levels.get("rich_zone")):
        cheap = _mget(levels, "cheap_zone")
        rich = _mget(levels, "rich_zone")
        add(
            "levels",
            f"cheap zone {_money(cheap.get('price_lo'))}-{_money(cheap.get('price_hi'))}, "
            f"rich zone {_money(rich.get('price_lo'))}-{_money(rich.get('price_hi'))}",
        )

    stack = _section(packet, "stack")
    if stack:
        if stack.get("published"):
            add("stack", "cross-sectional stack published (gates passed)")
        else:
            add("stack", f"stack not published: {stack.get('reason') or 'gates not met'}")

    return citations


# --------------------------------------------------------------------------
# Determinants / priced-in / zones / falsifiers
# --------------------------------------------------------------------------


def key_determinants(packet: Mapping[str, Any], *, limit: int = 6) -> list[dict[str, Any]]:
    """The named drivers of the read, most influential first."""
    determinants: list[dict[str, Any]] = []
    exposure = _section(packet, "exposure")
    betas = exposure.get("betas") if isinstance(exposure.get("betas"), Mapping) else {}
    scored = sorted(
        (
            (abs(_finite(v) or 0.0), str(k), _finite(v))
            for k, v in (betas or {}).items()
            if _finite(v) is not None
        ),
        key=lambda row: row[0],
        reverse=True,
    )
    for _mag, name, value in scored[:limit]:
        determinants.append(
            {
                "name": name,
                "explanation": f"basket beta {_num(value)} (exposure module)",
                "direction": "positive" if (value or 0) >= 0 else "negative",
            }
        )
    return determinants


def whats_priced_in(packet: Mapping[str, Any]) -> list[str]:
    """The 'what's priced in' panel: implied vs historical width and skew."""
    out: list[str] = []
    implied = _section(packet, "implied")
    by_h = _mget(implied, "by_horizon")
    for h in HORIZONS:
        block = by_h.get(str(h))
        if not isinstance(block, Mapping) or block.get("iv_atm") is None:
            continue
        width = _finite(block.get("width_ratio_vs_hist"))
        skew = _finite(block.get("skew_25d"))
        parts = [f"{h}m: ATM IV {_pct(block.get('iv_atm'))}"]
        if width is not None:
            if width > 1.15:
                parts.append(f"options price a {_num(width)}x wider move than history")
            elif width < 0.85:
                parts.append(f"options price a {_num(width)}x narrower move than history")
            else:
                parts.append(f"implied width in line with history ({_num(width)}x)")
        if skew is not None:
            parts.append(f"25-delta skew {_num(skew, digits=3)}")
        out.append("; ".join(parts))
    return out


def zones(packet: Mapping[str, Any]) -> dict[str, Any]:
    """The cheap/rich zones from the levels module."""
    levels = _section(packet, "levels")
    return {
        "cheap": levels.get("cheap_zone"),
        "rich": levels.get("rich_zone"),
        "poc": levels.get("poc"),
        "current_price": levels.get("current_price"),
    }


def falsifiers(packet: Mapping[str, Any]) -> list[str]:
    """Three concrete conditions that would prove the read wrong (SPEC §6.8)."""
    out: list[str] = []
    ticker = str(packet.get("ticker") or "this name")

    exposure = _section(packet, "exposure")
    determinants = key_determinants(packet, limit=2)
    if determinants:
        names = ", ".join(d["name"] for d in determinants)
        r2 = _finite(exposure.get("r2"))
        out.append(
            f"The exposure read attributes {ticker}'s moves mostly to {names} "
            f"(R² {_num(r2)}); if realized correlation to those legs breaks down over "
            "the next quarter, the 'what you're buying' thesis is falsified."
        )

    horizon = choose_posture_horizon(packet)
    if horizon is not None:
        block = _mget(_odds_by_horizon(packet), str(horizon))
        quantiles = _mget(block, "quantiles")
        q05 = _finite(quantiles.get("q05"))
        q95 = _finite(quantiles.get("q95"))
        if q05 is not None and q95 is not None:
            out.append(
                f"If the realized {horizon}-month return lands outside the "
                f"[{_pct(q05, sign=True)}, {_pct(q95, sign=True)}] band the odds distribution "
                "quotes, the distribution is miscalibrated for this regime."
            )

    state = _section(packet, "state")
    spy = _mget(state, "spy")
    cell = spy.get("cell")
    if cell:
        out.append(
            f"If SPY's volatility-trend state leaves the '{cell}' cell the base rates "
            "were conditioned on, the conditional odds no longer apply and must be recomputed."
        )

    # Backfills so there are always exactly three, drawn from real sections.
    if len(out) < 3:
        implied = _section(packet, "implied")
        by_h = _mget(implied, "by_horizon")
        for h in HORIZONS:
            b = by_h.get(str(h))
            if isinstance(b, Mapping) and b.get("width_ratio_vs_hist") is not None:
                out.append(
                    f"If realized volatility over the next {h} months diverges sharply from the "
                    f"{_num(b.get('width_ratio_vs_hist'))}x implied-vs-historical width, the "
                    "options market's priced view was wrong."
                )
                break
    while len(out) < 3:
        out.append(
            "If a subsequent 10-K or 10-Q materially changes the risk factors or guidance "
            "the business read relied on, the memo should be rebuilt."
        )
    return out[:3]


# --------------------------------------------------------------------------
# Deterministic markdown template (SPEC §6)
# --------------------------------------------------------------------------


def render_markdown(
    packet: Mapping[str, Any],
    *,
    posture: Mapping[str, Any],
    citations: Sequence[Mapping[str, Any]],
) -> str:
    """The fixed-template memo, used verbatim when no model is available."""
    ticker = str(packet.get("ticker") or "")
    profile = _section(packet, "profile")
    name = profile.get("name") or ticker
    as_of = packet.get("as_of") or "latest"
    lines: list[str] = []

    def cite(module: str) -> str:
        for c in citations:
            if c.get("module") == module:
                return f"[{c['id']}]"
        return ""

    # 1. Headline -----------------------------------------------------------
    lines.append(f"# Situate: {name} ({ticker}) — as of {as_of}")
    lines.append("")
    stance = str(posture.get("stance") or "balanced").replace("_", " ")
    lines.append(
        f"**Posture: {stance}** at {posture.get('horizon') or 'n/a'} months "
        f"(conviction {_num(posture.get('conviction'))}). {posture.get('one_line')}"
    )
    lines.append("")

    # 2. What you're buying -------------------------------------------------
    lines.append("## What you're buying")
    exposure = _section(packet, "exposure")
    if exposure:
        betas = exposure.get("betas") if isinstance(exposure.get("betas"), Mapping) else {}
        change12 = _mget(exposure, "change_12m")
        lines.append(
            f"The data suggests {ticker} is, in factor terms, an "
            f"R² {_num(exposure.get('r2'))} basket with "
            f"{_pct(exposure.get('idiosyncratic_share'))} of variance idiosyncratic "
            f"{cite('exposure')}."
        )
        lines.append("")
        lines.append("| Leg | Beta | SE | 12m change |")
        lines.append("| --- | ---: | ---: | ---: |")
        se = exposure.get("se") if isinstance(exposure.get("se"), Mapping) else {}
        for leg, beta in (betas or {}).items():
            lines.append(
                f"| {leg} | {_num(beta)} | {_num((se or {}).get(leg))} | "
                f"{_num((change12 or {}).get(leg))} |"
            )
        factor = exposure.get("factor") if isinstance(exposure.get("factor"), Mapping) else {}
        loadings = _mget(factor, "loadings")
        if loadings:
            loading_bits = ", ".join(f"{k} {_num(v)}" for k, v in loadings.items())
            lines.append("")
            lines.append(f"Named-factor (Fama-French) loadings: {loading_bits} {cite('exposure')}.")
    else:
        lines.append(f"Exposure unavailable: {packet.get('exposure_error') or 'not computed'}.")
    lines.append("")

    # 3. State --------------------------------------------------------------
    lines.append("## State")
    state = _section(packet, "state")
    if state:
        spy = _mget(state, "spy")
        tk = _mget(state, "ticker")
        context = _mget(state, "context")
        lines.append(
            f"The data suggests SPY is in the '{spy.get('cell')}' cell and {ticker} in the "
            f"'{tk.get('cell')}' cell (2x2 volatility x trend) {cite('state')}."
        )
        hmm = state.get("hmm") if isinstance(state.get("hmm"), Mapping) else None
        if hmm and isinstance(hmm.get("probs"), Mapping):
            probs = ", ".join(f"{k} {_pct(v, digits=0)}" for k, v in hmm["probs"].items())
            lines.append(f"HMM second opinion (SPY): {probs} — label '{hmm.get('label')}'.")
        if context:
            lines.append(
                f"Context: VIX {_num(context.get('vix_level'))} "
                f"({_pct(context.get('vix_pct'), digits=0)} pct-ile), "
                f"HY OAS {_pct(context.get('hy_oas_pct'), digits=0)} pct-ile, "
                f"10y-2y {_num(context.get('curve_10y_2y'))}."
            )
    else:
        lines.append(f"State unavailable: {packet.get('state_error') or 'not computed'}.")
    lines.append("")

    # 4. Odds by horizon ----------------------------------------------------
    lines.append("## Odds by horizon")
    by_h = _odds_by_horizon(packet)
    base_by_h = (_section(packet, "base_rates").get("by_horizon") or {})
    implied_by_h = (_section(packet, "implied").get("by_horizon") or {})
    if by_h:
        lines.append("The unconditional base rate sits beside every conditional (shrunk) number "
                     f"and the option-implied median {cite('base_rates')}{cite('implied')}.")
        lines.append("")
        lines.append("| Horizon | Uncond q50 | Cond-shrunk q50 | Implied q50 | P(up) | Source |")
        lines.append("| --- | ---: | ---: | ---: | ---: | --- |")
        for h in HORIZONS:
            block = by_h.get(str(h)) or {}
            quantiles = _mget(block, "quantiles")
            br_block = base_by_h.get(str(h)) if isinstance(base_by_h.get(str(h)), Mapping) else {}
            uncond = (br_block.get("uncond") or {}) if isinstance(br_block, Mapping) else {}
            imp_block = _mget(implied_by_h, str(h))
            imp_rw = imp_block.get("rw_quantiles") or imp_block.get("quantiles") or {}
            lines.append(
                f"| {h}m | {_pct(uncond.get('q50'), sign=True)} | "
                f"{_pct(quantiles.get('q50'), sign=True)} | "
                f"{_pct(imp_rw.get('q50'), sign=True)} | "
                f"{_pct(block.get('p_up'), digits=0)} | {block.get('source') or 'n/a'} |"
            )
        # Flag disagreements.
        disagreements = _implied_history_disagreements(packet)
        if disagreements:
            lines.append("")
            joined = "; ".join(disagreements)
            lines.append(f"**Where implied and history disagree:** {joined}.")
    else:
        lines.append("Odds unavailable: no base-rate or implied distribution was built.")
    lines.append("")

    # 5. The business -------------------------------------------------------
    lines.append("## The business")
    fundamentals = _section(packet, "fundamentals")
    if fundamentals:
        momentum = _mget(fundamentals, "momentum")
        quality = _mget(fundamentals, "quality")
        value_z = _mget(fundamentals, "value_z")
        flags = _mget(fundamentals, "trajectory_flags")
        lines.append(
            f"Momentum 12-1 {_pct(momentum.get('ret_12_1'), sign=True)}, "
            f"1-month reversal {_pct(momentum.get('ret_1m_reversal'), sign=True)}; "
            f"quality gp/assets {_num(quality.get('gp_to_assets'))}, "
            f"net debt/EBITDA {_num(quality.get('net_debt_ebitda'))} {cite('fundamentals')}."
        )
        if value_z:
            basis = value_z.get("basis")
            zbits = ", ".join(
                f"{k} z {_num(v)}" for k, v in value_z.items()
                if k != "basis" and _finite(v) is not None
            )
            if zbits:
                lines.append(f"Value z-scores ({basis}): {zbits} {cite('fundamentals')}.")
        if flags:
            lines.append(
                f"Trajectory flags: revenue accelerating {flags.get('rev_accel')}, "
                f"margin accelerating {flags.get('margin_accel')}."
            )
        if fundamentals.get("revisions") is None:
            lines.append(
                "Revision momentum and PEAD are unavailable: "
                f"{fundamentals.get('revisions_error') or 'no estimate feed'}."
            )
    else:
        reason = packet.get("fundamentals_error") or "not computed"
        lines.append(f"Fundamentals unavailable: {reason}.")

    text = _section(packet, "text")
    changes = text.get("filing_changes") or []
    if changes and isinstance(changes[0], Mapping):
        top = changes[0]
        lines.append("")
        lines.append(
            f"Filing diff: material-change score "
            f"{_num(top.get('material_change_score'), digits=1)} in "
            f"{top.get('section') or 'the latest filing'} {cite('text')}."
        )
        for risk in (top.get("new_risks") or [])[:2]:
            if isinstance(risk, Mapping) and risk.get("quote"):
                lines.append(f"> New risk: “{str(risk.get('quote'))[:240]}”")
    events = text.get("events") if isinstance(text.get("events"), list) else []
    if events:
        lines.append("")
        lines.append("Recent dated events:")
        for event in events[:3]:
            if isinstance(event, Mapping):
                lines.append(
                    f"- {event.get('date')}: {event.get('headline') or event.get('type')} "
                    f"(sentiment {event.get('sentiment')})"
                )
    lines.append("")

    # 6. Scenarios ----------------------------------------------------------
    lines.append("## Scenarios")
    scenarios = _section(packet, "scenarios")
    if scenarios:
        for scenario_name in ("bull", "neutral", "bear"):
            block = _mget(scenarios, scenario_name)
            if not block:
                continue
            horizons = block.get("horizons") if isinstance(block.get("horizons"), Mapping) else {}
            bits = []
            for h in (3, 6, 12):
                hblock = _mget(horizons, str(h))
                bits.append(f"{h}m {_pct(hblock.get('quantile'), sign=True)}")
            drivers = []
            for h in (3, 6, 12):
                hblock = _mget(horizons, str(h))
                hdrivers = hblock.get("drivers")
                if isinstance(hdrivers, list) and hdrivers:
                    drivers = [d.get("name") for d in hdrivers if isinstance(d, Mapping)]
                    break
            lines.append(
                f"- **{scenario_name.title()}** ({block.get('state')}): "
                + ", ".join(bits)
                + (f"; top drivers {', '.join(str(d) for d in drivers)}" if drivers else "")
            )
    else:
        lines.append("Scenarios unavailable: the odds distribution was not built.")
    lines.append("")

    # 7. Zones --------------------------------------------------------------
    lines.append("## Zones")
    z = zones(packet)
    if z.get("cheap") or z.get("rich"):
        cheap = _mget(z, "cheap")
        rich = _mget(z, "rich")
        lines.append(
            f"Cheap zone {_money(cheap.get('price_lo'))}-{_money(cheap.get('price_hi'))}, "
            f"rich zone {_money(rich.get('price_lo'))}-{_money(rich.get('price_hi'))} "
            f"(option-implied quantiles at {cheap.get('horizon')}) {cite('levels')}."
        )
        lines.append(
            f"Point of control {_money(z.get('poc'))}; "
            f"current price {_money(z.get('current_price'))}."
        )
        zone_drivers = key_determinants(packet, limit=2)
        if zone_drivers:
            lines.append(
                "A large move in "
                + " or ".join(str(d["name"]) for d in zone_drivers)
                + " is what would shift these zones."
            )
    else:
        reason = packet.get("levels_error") or "no implied distribution"
        lines.append(f"Zones unavailable: {reason}.")
    lines.append("")

    # 8. What would prove this wrong ---------------------------------------
    lines.append("## What would prove this wrong")
    for i, item in enumerate(falsifiers(packet), start=1):
        lines.append(f"{i}. {item}")
    lines.append("")

    # 9. Confidence and caveats --------------------------------------------
    lines.append("## Confidence and caveats")
    lines.extend(_confidence_lines(packet, posture))
    lines.append("")

    # 10. Appendix ----------------------------------------------------------
    lines.append("## Appendix")
    meta = _section(packet, "meta")
    versions = meta.get("versions") if isinstance(meta.get("versions"), Mapping) else {}
    if versions:
        version_bits = ", ".join(f"{k} v{v}" for k, v in versions.items())
        lines.append(f"Module versions: {version_bits}.")
    if citations:
        lines.append("")
        lines.append(render_citations(citations))
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def _implied_history_disagreements(packet: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    base_by_h = _section(packet, "base_rates").get("by_horizon") or {}
    implied_by_h = _section(packet, "implied").get("by_horizon") or {}
    for h in HORIZONS:
        br_block = base_by_h.get(str(h)) if isinstance(base_by_h.get(str(h)), Mapping) else {}
        shrunk = (br_block.get("shrunk") or {}) if isinstance(br_block, Mapping) else {}
        imp_block = _mget(implied_by_h, str(h))
        imp_rw = (imp_block.get("rw_quantiles") or {}) if isinstance(imp_block, Mapping) else {}
        hist_med = _finite(shrunk.get("q50"))
        imp_med = _finite(imp_rw.get("q50"))
        width = _finite(imp_block.get("width_ratio_vs_hist"))
        if width is not None and (width > 1.25 or width < 0.8):
            out.append(f"{h}m implied move is {_num(width)}x the historical conditional band")
        if hist_med is not None and imp_med is not None and abs(hist_med - imp_med) > 0.03:
            out.append(
                f"{h}m historical median {_pct(hist_med, sign=True)} vs "
                f"implied {_pct(imp_med, sign=True)}"
            )
    return out[:4]


def _confidence_lines(packet: Mapping[str, Any], posture: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    horizon = posture.get("horizon")
    if horizon is not None:
        block = _odds_by_horizon(packet).get(str(horizon)) or {}
        base_by_h = (_section(packet, "base_rates").get("by_horizon") or {})
        br_block = _mget(base_by_h, str(horizon))
        shrunk = (br_block.get("shrunk") or {}) if isinstance(br_block, Mapping) else {}
        lines.append(
            f"At the {horizon}-month posture horizon: effective sample n_eff "
            f"{_num(shrunk.get('n_eff'), digits=1)}, shrink weight "
            f"{_num(block.get('shrink_w'))} "
            "(0 = fully shrunk to the base rate, 1 = fully conditional)."
        )
    stack = _section(packet, "stack")
    if stack:
        if stack.get("published"):
            lines.append(
                "Stack gate status: PUBLISHED — the cross-sectional model passed its "
                "OOS IC and deflated-Sharpe gates."
            )
        else:
            lines.append(
                f"Stack gate status: NOT published ({stack.get('reason') or 'gates not met'}); "
                "the odds fall back to base rates plus the option-implied distribution."
            )
    meta = _section(packet, "meta")
    raw_unavailable = meta.get("unavailable")
    unavailable = raw_unavailable if isinstance(raw_unavailable, list) else []
    gaps = []
    for row in unavailable[:6]:
        if isinstance(row, Mapping):
            gaps.append(f"{row.get('source')}: {row.get('reason')}")
    if gaps:
        lines.append("Data gaps: " + "; ".join(gaps) + ".")
    fundamentals = _section(packet, "fundamentals")
    if fundamentals and fundamentals.get("revisions") is None:
        lines.append(
            "No consensus-estimate feed is available, so analyst revision momentum "
            "and PEAD are omitted."
        )
    lines.append(DISCLAIMER)
    return lines


# --------------------------------------------------------------------------
# Projection (LLM + chat briefing)
# --------------------------------------------------------------------------


def project_packet(packet: Mapping[str, Any], *, max_chars: int = DEFAULT_PROJECTION_CHARS) -> str:
    """A bounded briefing an LLM (memo or chat) reads: the deterministic memo
    body plus the citation catalogue, so every number and its source is present."""
    posture = derive_posture(packet)
    citations = build_citations(packet)
    body = render_markdown(packet, posture=posture, citations=citations)
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n\n[...briefing truncated...]"
    return body


# --------------------------------------------------------------------------
# Memo assembly
# --------------------------------------------------------------------------


def fallback_memo(packet: Mapping[str, Any], *, reason: str) -> dict[str, Any]:
    """A complete, honest memo assembled from the packet with no model call."""
    posture = derive_posture(packet)
    citations = build_citations(packet)
    text = render_markdown(packet, posture=posture, citations=citations)
    return {
        "posture": {
            "stance": posture["stance"],
            "horizon": posture["horizon"],
            "conviction": posture["conviction"],
            "one_line": posture["one_line"],
        },
        "text": text,
        "falsifiers": falsifiers(packet),
        "key_determinants": key_determinants(packet),
        "whats_priced_in": whats_priced_in(packet),
        "citations": citations,
        "zones": {"cheap": zones(packet).get("cheap"), "rich": zones(packet).get("rich")},
        "model": None,
        "method": "deterministic",
        "reason": reason,
        "ticker": str(packet.get("ticker") or ""),
        "engine": ENGINE_NAME,
        "engine_version": ENGINE_VERSION,
        "version": MEMO_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
    }


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

    With no model the deterministic template is returned (never an empty
    section). With a model, the prose is rewritten from the same briefing, but
    the posture, falsifiers, determinants, zones and citations are always the
    engine's own — the model may not invent a number or a citation id.
    """
    generator = text_generator
    if generator is None:
        if not api_key:
            return fallback_memo(packet, reason="no text generator and no ANTHROPIC_API_KEY")
        from app.anthropic import AnthropicTextClient

        generator = AnthropicTextClient(api_key=api_key, model=text_model)

    posture = derive_posture(packet)
    citations = build_citations(packet)
    citation_ids = {c["id"] for c in citations}
    briefing = project_packet(packet, max_chars=projection_chars)

    prompt = (
        f"{briefing}\n\n"
        "## Engine-derived posture (you may rephrase but not overturn without stating why)\n"
        f"- posture: {posture['stance']} at {posture['horizon']} months, "
        f"conviction {posture['conviction']}\n"
        f"- one line: {posture['one_line']}\n\n"
        "Write the memo now, keeping the fixed section order. Return strict JSON only."
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
        return memo

    stance = str(parsed.get("posture") or "").strip().lower()
    if stance not in POSTURES:
        stance = posture["stance"]
    horizon = parsed.get("horizon")
    try:
        horizon = int(horizon) if horizon is not None else posture["horizon"]
    except (TypeError, ValueError):
        horizon = posture["horizon"]
    conviction = _finite(parsed.get("conviction"))
    conviction = posture["conviction"] if conviction is None else max(0.0, min(1.0, conviction))

    text = str(parsed.get("text") or "").strip()
    # Same guard as Prism: a model-authored citation list renumbers ids so a
    # returned "[Cn]" would carry the wrong claim. Strip it and refuse ids whose
    # gloss does not match the engine catalogue.
    text, model_block = strip_model_citations(text)
    mismatched = mismatched_citation_ids(citation_glosses(model_block), citations)
    if mismatched:
        memo = fallback_memo(
            packet,
            reason=f"citation ids did not resolve: model renumbered {', '.join(mismatched)}",
        )
        memo["model"] = model
        return memo

    body = text.rstrip()
    if body.endswith(DISCLAIMER):
        body = body[: -len(DISCLAIMER)].rstrip()
    if citations:
        body = f"{body}\n\n{render_citations(citations)}"
    text = f"{body}\n\n{DISCLAIMER}"

    # Falsifiers: prefer the model's three when they are non-empty strings, else
    # the engine's. Never fewer than three.
    model_falsifiers = [
        str(item).strip()
        for item in (parsed.get("falsifiers") or [])
        if isinstance(item, str) and item.strip()
    ]
    resolved_falsifiers = model_falsifiers[:3] if len(model_falsifiers) >= 3 else falsifiers(packet)

    used_ids = sorted(
        {str(v) for v in (parsed.get("citation_ids") or [])} & citation_ids,
        key=lambda v: int(v[1:]),
    )

    return {
        "posture": {
            "stance": stance,
            "horizon": horizon,
            "conviction": round(conviction, 3),
            "one_line": str(parsed.get("one_line") or posture["one_line"]),
        },
        "text": text,
        "falsifiers": resolved_falsifiers,
        "key_determinants": key_determinants(packet),
        "whats_priced_in": whats_priced_in(packet),
        "citations": citations,
        "citation_ids_used": used_ids,
        "zones": {"cheap": zones(packet).get("cheap"), "rich": zones(packet).get("rich")},
        "model": model,
        "method": "model",
        "reason": None,
        "ticker": str(packet.get("ticker") or ""),
        "engine": ENGINE_NAME,
        "engine_version": ENGINE_VERSION,
        "version": MEMO_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "projection_chars": len(briefing),
        "model_output_chars": len(raw),
    }
