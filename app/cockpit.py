from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from app.analysis import scanner_row, summarize_stock
from app.charts import (
    calculate_auction_observation,
    calculate_flow_compass_indicator,
    calculate_ridge_growth_strategy,
)
from app.market_data import MarketDataClient


def build_cockpit_row(
    client: MarketDataClient,
    ticker: str,
    *,
    period: str = "1y",
    sec_client: Any | None = None,
    exa_client: Any | None = None,
    include_torque: bool = False,
) -> dict[str, Any]:
    summary = summarize_stock(client, ticker)
    symbol = str(summary["ticker"])
    scanner = scanner_row(summary)
    history = client.get_history(symbol, period=period, interval="1d")
    _ridge_frame, ridge = calculate_ridge_growth_strategy(history)
    _flow_frame, flow = calculate_flow_compass_indicator(history)
    auction = calculate_auction_observation(history)

    torque_meta: dict[str, Any] | None = None
    reclass_meta: dict[str, Any] | None = None
    if include_torque:
        torque_meta, reclass_meta = _compute_fundamentals(
            client=client,
            symbol=symbol,
            history=history,
            sec_client=sec_client,
            exa_client=exa_client,
        )

    score = cockpit_score(
        scanner=scanner,
        ridge=ridge,
        flow=flow,
        auction=auction,
        torque=torque_meta,
    )

    row: dict[str, Any] = {
        "rank": 0,
        "ticker": symbol,
        "name": summary.get("name"),
        "sector": summary.get("sector"),
        "industry": summary.get("industry"),
        "price": summary.get("price"),
        "change_percent": summary.get("change_percent"),
        "annual_volatility": summary.get("annual_volatility"),
        "trend_50d": summary.get("trend_50d"),
        "distance_from_52w_high": scanner.get("distance_from_52w_high"),
        "distance_from_52w_low": scanner.get("distance_from_52w_low"),
        "scanner_score": scanner.get("score"),
        "score": score,
        "lane": cockpit_lane(score, torque=torque_meta),
        "setup": cockpit_setup(ridge, flow, auction, torque=torque_meta),
        "provider": history.provider,
        "provider_note": history.note,
        "summary": summary,
        "ridge": {
            "state": ridge.get("state"),
            "recommendation": ridge.get("recommendation"),
            "ending_equity": ridge.get("ending_equity"),
            "total_return": ridge.get("total_return"),
            "max_drawdown": ridge.get("max_drawdown"),
            "trend_confirmed": ridge.get("trend_confirmed"),
            "open_position_return": ridge.get("open_position_return"),
        },
        "flow": {
            "state": flow.get("state"),
            "score": flow.get("score"),
            "signal": flow.get("signal"),
            "volume_score": flow.get("volume_score"),
            "trend_score": flow.get("trend_score"),
            "momentum_score": flow.get("momentum_score"),
            "value_score": flow.get("value_score"),
            "rvi_score": flow.get("rvi_score"),
            "fresh_long": flow.get("fresh_long"),
            "fresh_short": flow.get("fresh_short"),
        },
        "auction": {
            "location": auction.get("location"),
            "poc": auction.get("poc"),
            "vah": auction.get("vah"),
            "val": auction.get("val"),
            "distance_to_poc": auction.get("distance_to_poc"),
        },
    }
    if torque_meta is not None:
        row["torque"] = {
            "total_score": torque_meta.get("total_score"),
            "stage_label": torque_meta.get("stage_label"),
            "recommendation": torque_meta.get("recommendation"),
            "target_zone": torque_meta.get("target_zone"),
        }
    if reclass_meta is not None:
        row["reclassification"] = {
            "old_noun": reclass_meta.get("old_noun"),
            "primary_new_verb": reclass_meta.get("primary_new_verb"),
            "functional_layer": reclass_meta.get("functional_layer"),
            "proof_stage": reclass_meta.get("proof_stage"),
            "proof_stage_label": reclass_meta.get("proof_stage_label"),
            "reclassification_gap": reclass_meta.get("reclassification_gap"),
            "target_low": reclass_meta.get("target_low"),
            "target_mid": reclass_meta.get("target_mid"),
            "target_high": reclass_meta.get("target_high"),
        }
    return row


def _compute_fundamentals(
    *,
    client: MarketDataClient,
    symbol: str,
    history: Any,
    sec_client: Any | None,
    exa_client: Any | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Pull torque + reclassification for a single ticker. Tolerant of missing modules/data."""
    try:
        profile = client.get_profile(symbol)
    except Exception:
        profile = None

    sec_trend_pack: dict[str, Any] | None = None
    try:
        from app.sec_trend import build_sec_trend_pack

        sec_trend_pack = build_sec_trend_pack(sec_client, symbol, quarters=8)
    except Exception:
        sec_trend_pack = None

    exa_research: dict[str, Any] | None = None
    try:
        from app.exa import build_research_pack

        company_name = ""
        if profile is not None:
            company_name = str(
                profile.get("longName") or profile.get("shortName") or symbol
            )
        else:
            company_name = symbol
        industry = profile.get("industry") if profile is not None else None
        sector = profile.get("sector") if profile is not None else None
        exa_research = build_research_pack(
            exa_client,
            symbol,
            company_name,
            industry=industry,
            sector=sector,
        )
    except Exception:
        exa_research = None

    sec_source_pack: dict[str, Any] | None = None
    try:
        from app.tools import build_sec_source_pack

        sec_source_pack = build_sec_source_pack(sec_client, symbol)
    except Exception:
        sec_source_pack = None

    torque_dict: dict[str, Any] | None = None
    try:
        from app.torque import compute_torque_score

        market_cap = profile.get("marketCap") if profile is not None else None
        result = compute_torque_score(
            history=history,
            sec_trend=sec_trend_pack,
            profile=profile,
            market_cap=market_cap,
        )
        torque_dict = _to_dict(result)
    except Exception:
        torque_dict = None

    reclass_dict: dict[str, Any] | None = None
    try:
        from app.reclassification import score_reclassification

        result = score_reclassification(
            ticker=symbol,
            profile=profile,
            history=history,
            sec_trend=sec_trend_pack,
            sec_source_pack=sec_source_pack,
            exa_research=exa_research,
            torque_result=torque_dict,
        )
        reclass_dict = _to_dict(result)
    except Exception:
        reclass_dict = None

    return torque_dict, reclass_dict


def _to_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        try:
            return asdict(value)
        except Exception:
            return None
    if hasattr(value, "__dict__"):
        try:
            return dict(value.__dict__)
        except Exception:
            return None
    return None


def cockpit_score(
    *,
    scanner: dict[str, Any],
    ridge: dict[str, Any],
    flow: dict[str, Any],
    auction: dict[str, Any],
    torque: dict[str, Any] | None = None,
) -> float:
    score = float_value(scanner.get("score")) * 0.65
    score += float_value(flow.get("score")) * 0.35
    score += ridge_recommendation_points(str(ridge.get("recommendation") or ""))
    score += ridge_state_points(str(ridge.get("state") or ""))
    score += auction_location_points(str(auction.get("location") or ""))

    if bool(flow.get("fresh_long")):
        score += 10.0
    if bool(flow.get("fresh_short")):
        score -= 14.0

    score += float_value(ridge.get("total_return")) * 30.0
    score -= abs(min(float_value(ridge.get("max_drawdown")), 0.0)) * 35.0

    if torque is not None:
        score += torque_contribution(torque)

    return round(score, 2)


def torque_contribution(torque: dict[str, Any]) -> float:
    """Fold the 0-100 torque score (recentered to -50..+50) and stage bonus into cockpit score."""
    total = float_value(torque.get("total_score"))
    centered = total - 50.0
    contribution = centered * 0.4
    stage_label = str(torque.get("stage_label") or "").lower()
    stage_bonus = {
        "coiled spring": 12.0,
        "inflecting": 8.0,
        "proof phase": 4.0,
        "renaming phase": -2.0,
        "extended": -10.0,
        "no setup": -4.0,
    }.get(stage_label, 0.0)
    return contribution + stage_bonus


def cockpit_lane(score: float, *, torque: dict[str, Any] | None = None) -> str:
    if torque is not None:
        stage_label = str(torque.get("stage_label") or "").lower()
        if stage_label in {"coiled spring", "inflecting"} and score >= 25:
            return "Misclassified Inflection"
    if score >= 45:
        return "Priority"
    if score >= 18:
        return "Watch"
    if score <= -18:
        return "Risk"
    return "Review"


def cockpit_setup(
    ridge: dict[str, Any],
    flow: dict[str, Any],
    auction: dict[str, Any],
    *,
    torque: dict[str, Any] | None = None,
) -> str:
    recommendation = str(ridge.get("recommendation") or "No ridge read")
    flow_state = str(flow.get("state") or "No flow read")
    location = str(auction.get("location") or "unknown auction location")
    base = f"{recommendation} / {flow_state} / {location}"
    if torque is not None:
        stage = str(torque.get("stage_label") or "").strip()
        if stage:
            return f"{base} / {stage}"
    return base


def ridge_recommendation_points(recommendation: str) -> float:
    return {
        "BUY": 30.0,
        "BUY SETUP": 22.0,
        "HOLD LONG": 18.0,
        "WATCH": 6.0,
        "CASH": -8.0,
        "SELL": -30.0,
    }.get(recommendation.upper(), 0.0)


def ridge_state_points(state: str) -> float:
    return {
        "LONG": 8.0,
        "WATCH": 2.0,
        "CASH": -5.0,
    }.get(state.upper(), 0.0)


def auction_location_points(location: str) -> float:
    return {
        "above value": 8.0,
        "inside value": 2.0,
        "below value": -8.0,
    }.get(location.lower(), 0.0)


def float_value(value: Any) -> float:
    if isinstance(value, int | float):
        return float(value)
    return 0.0
