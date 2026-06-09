from __future__ import annotations

from typing import Any

from app.analysis import scanner_row, summarize_stock
from app.charts import (
    calculate_auction_observation,
    calculate_flow_compass_indicator,
    calculate_ridge_growth_strategy,
)
from app.market_data import MarketDataClient


def build_cockpit_row(
    client: MarketDataClient, ticker: str, *, period: str = "1y"
) -> dict[str, Any]:
    summary = summarize_stock(client, ticker)
    symbol = str(summary["ticker"])
    scanner = scanner_row(summary)
    history = client.get_history(symbol, period=period, interval="1d")
    _ridge_frame, ridge = calculate_ridge_growth_strategy(history)
    _flow_frame, flow = calculate_flow_compass_indicator(history)
    auction = calculate_auction_observation(history)
    score = cockpit_score(scanner=scanner, ridge=ridge, flow=flow, auction=auction)

    return {
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
        "lane": cockpit_lane(score),
        "setup": cockpit_setup(ridge, flow, auction),
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


def cockpit_score(
    *,
    scanner: dict[str, Any],
    ridge: dict[str, Any],
    flow: dict[str, Any],
    auction: dict[str, Any],
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
    return round(score, 2)


def cockpit_lane(score: float) -> str:
    if score >= 45:
        return "Priority"
    if score >= 18:
        return "Watch"
    if score <= -18:
        return "Risk"
    return "Review"


def cockpit_setup(
    ridge: dict[str, Any], flow: dict[str, Any], auction: dict[str, Any]
) -> str:
    recommendation = str(ridge.get("recommendation") or "No ridge read")
    flow_state = str(flow.get("state") or "No flow read")
    location = str(auction.get("location") or "unknown auction location")
    return f"{recommendation} / {flow_state} / {location}"


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
