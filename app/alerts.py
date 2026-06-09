from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from app.cockpit import float_value

DEFAULT_ALERT_LIMIT = 12
MAX_ALERT_LIMIT = 50
DEFAULT_VOLATILITY_THRESHOLD = 0.55

SEVERITY_ORDER = {"High": 3, "Medium": 2, "Info": 1}


def build_alert_digest(
    rows: list[dict[str, Any]],
    *,
    max_alerts: int = DEFAULT_ALERT_LIMIT,
    volatility_threshold: float = DEFAULT_VOLATILITY_THRESHOLD,
) -> dict[str, Any]:
    alerts: list[dict[str, Any]] = []
    for row in rows:
        alerts.extend(row_alerts(row, volatility_threshold=volatility_threshold))

    alerts.sort(key=alert_sort_key)
    alerts = alerts[:max_alerts]
    digest = digest_payload(rows, alerts)
    return {"alerts": alerts, "digest": digest}


def row_alerts(
    row: dict[str, Any], *, volatility_threshold: float
) -> list[dict[str, Any]]:
    ticker = str(row.get("ticker") or "UNKNOWN")
    lane = str(row.get("lane") or "Review")
    score = float_value(row.get("score"))
    rank = int(float_value(row.get("rank")))
    flow = nested_dict(row.get("flow"))
    ridge = nested_dict(row.get("ridge"))
    auction = nested_dict(row.get("auction"))
    annual_volatility = float_value(row.get("annual_volatility"))
    alerts: list[dict[str, Any]] = []

    if lane == "Priority":
        alerts.append(
            alert_payload(
                row,
                kind="priority-setup",
                severity="High",
                category="Setup",
                title="Priority setup",
                message=(
                    f"{ticker} is ranked #{rank} with a {score:.1f} cockpit score "
                    f"and {str(row.get('setup') or 'mixed setup')}."
                ),
                action="Review Ridge and Flow confirmation before sizing.",
            )
        )

    if lane == "Risk":
        alerts.append(
            alert_payload(
                row,
                kind="risk-lane",
                severity="High",
                category="Risk",
                title="Risk lane",
                message=f"{ticker} is in the Risk lane with a {score:.1f} cockpit score.",
                action="Check open exposure and wait for the state to improve.",
            )
        )

    if bool(flow.get("fresh_short")):
        alerts.append(
            alert_payload(
                row,
                kind="fresh-short-flow",
                severity="High",
                category="Flow",
                title="Fresh short flow",
                message=f"{ticker} printed a fresh short Flow Compass shift.",
                action="Treat long ideas as impaired until flow stabilizes.",
            )
        )
    elif bool(flow.get("fresh_long")):
        alerts.append(
            alert_payload(
                row,
                kind="fresh-long-flow",
                severity="Medium",
                category="Flow",
                title="Fresh long flow",
                message=f"{ticker} printed a fresh long Flow Compass shift.",
                action="Compare entry quality against auction value and Ridge state.",
            )
        )

    recommendation = str(ridge.get("recommendation") or "").upper()
    if recommendation in {"BUY", "BUY SETUP", "SELL"}:
        alerts.append(ridge_alert(row, recommendation))

    location = str(auction.get("location") or "").lower()
    if location in {"above value", "below value"}:
        alerts.append(auction_alert(row, location))

    if annual_volatility >= volatility_threshold:
        alerts.append(
            alert_payload(
                row,
                kind="high-volatility",
                severity="Medium",
                category="Volatility",
                title="High volatility",
                message=(
                    f"{ticker} is running at {format_percent(annual_volatility)} "
                    "annualized volatility."
                ),
                action="Size risk with wider expected range in mind.",
            )
        )

    return alerts


def ridge_alert(row: dict[str, Any], recommendation: str) -> dict[str, Any]:
    ticker = str(row.get("ticker") or "UNKNOWN")
    if recommendation == "SELL":
        return alert_payload(
            row,
            kind="ridge-sell",
            severity="High",
            category="Ridge",
            title="Ridge sell",
            message=f"{ticker} has a Ridge SELL recommendation.",
            action="Review exits or avoid fresh long exposure.",
        )
    if recommendation == "BUY":
        return alert_payload(
            row,
            kind="ridge-buy",
            severity="High",
            category="Ridge",
            title="Ridge buy",
            message=f"{ticker} has a Ridge BUY recommendation.",
            action="Confirm flow, auction location, and current price before entry.",
        )
    return alert_payload(
        row,
        kind="ridge-buy-setup",
        severity="Medium",
        category="Ridge",
        title="Ridge buy setup",
        message=f"{ticker} has a Ridge BUY SETUP recommendation.",
        action="Watch for confirmation instead of forcing the entry.",
    )


def auction_alert(row: dict[str, Any], location: str) -> dict[str, Any]:
    ticker = str(row.get("ticker") or "UNKNOWN")
    if location == "above value":
        return alert_payload(
            row,
            kind="auction-above-value",
            severity="Medium",
            category="Auction",
            title="Above value",
            message=f"{ticker} is accepting above auction value.",
            action="Look for continuation only if flow remains supportive.",
        )
    return alert_payload(
        row,
        kind="auction-below-value",
        severity="Medium",
        category="Auction",
        title="Below value",
        message=f"{ticker} is trading below auction value.",
        action="Require reclaim evidence before treating weakness as opportunity.",
    )


def alert_payload(
    row: dict[str, Any],
    *,
    kind: str,
    severity: str,
    category: str,
    title: str,
    message: str,
    action: str,
) -> dict[str, Any]:
    ticker = str(row.get("ticker") or "UNKNOWN")
    rank = int(float_value(row.get("rank")))
    score = float_value(row.get("score"))
    return {
        "id": f"{ticker.lower()}-{kind}",
        "ticker": ticker,
        "rank": rank,
        "lane": str(row.get("lane") or "Review"),
        "score": score,
        "severity": severity,
        "category": category,
        "title": title,
        "message": message,
        "action": action,
    }


def digest_payload(
    rows: list[dict[str, Any]], alerts: list[dict[str, Any]]
) -> dict[str, Any]:
    severity_counts = Counter(str(alert["severity"]) for alert in alerts)
    category_counts = Counter(str(alert["category"]) for alert in alerts)
    lane_counts = Counter(str(row.get("lane") or "Review") for row in rows)
    priority_tickers = tickers_for_lane(rows, "Priority")
    risk_tickers = tickers_for_lane(rows, "Risk")
    flow_shift_tickers = flow_shift_names(rows)

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "headline": digest_headline(alerts, len(rows)),
        "summary": digest_summary(priority_tickers, risk_tickers, flow_shift_tickers),
        "severity_counts": dict(severity_counts),
        "category_counts": dict(category_counts),
        "lane_counts": dict(lane_counts),
        "priority_tickers": priority_tickers,
        "risk_tickers": risk_tickers,
        "flow_shift_tickers": flow_shift_tickers,
        "next_steps": next_steps(alerts, priority_tickers, risk_tickers),
    }


def digest_headline(alerts: list[dict[str, Any]], row_count: int) -> str:
    high_count = sum(1 for alert in alerts if alert["severity"] == "High")
    if high_count:
        noun = "alert" if high_count == 1 else "alerts"
        return f"{high_count} high-priority {noun} across {row_count} names"
    if alerts:
        noun = "alert" if len(alerts) == 1 else "alerts"
        return f"{len(alerts)} watchlist {noun} across {row_count} names"
    return f"No high-signal alerts across {row_count} names"


def digest_summary(
    priority_tickers: list[str],
    risk_tickers: list[str],
    flow_shift_tickers: list[str],
) -> str:
    parts = []
    if priority_tickers:
        parts.append(f"Priority: {', '.join(priority_tickers[:5])}")
    if risk_tickers:
        parts.append(f"Risk: {', '.join(risk_tickers[:5])}")
    if flow_shift_tickers:
        parts.append(f"Flow shifts: {', '.join(flow_shift_tickers[:5])}")
    if not parts:
        return "No names crossed the alert rules in this run."
    return " | ".join(parts)


def next_steps(
    alerts: list[dict[str, Any]],
    priority_tickers: list[str],
    risk_tickers: list[str],
) -> list[str]:
    steps = []
    if risk_tickers:
        steps.append("Review risk alerts before adding exposure.")
    if priority_tickers:
        steps.append("Open priority setups in Cockpit for confirmation.")
    if alerts:
        steps.append("Export or save the digest with the research library.")
    if not steps:
        steps.append("Keep the current list on watch; no immediate action fired.")
    return steps


def tickers_for_lane(rows: list[dict[str, Any]], lane: str) -> list[str]:
    return [
        str(row.get("ticker"))
        for row in rows
        if row.get("ticker") and str(row.get("lane") or "") == lane
    ]


def flow_shift_names(rows: list[dict[str, Any]]) -> list[str]:
    names = []
    for row in rows:
        flow = nested_dict(row.get("flow"))
        if bool(flow.get("fresh_long")) or bool(flow.get("fresh_short")):
            names.append(str(row.get("ticker")))
    return names


def nested_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def alert_sort_key(alert: dict[str, Any]) -> tuple[int, float, int, str]:
    severity = SEVERITY_ORDER.get(str(alert.get("severity")), 0)
    score = float_value(alert.get("score"))
    rank = int(float_value(alert.get("rank")))
    return (-severity, -score, rank, str(alert.get("id") or ""))


def format_percent(value: float) -> str:
    return f"{value * 100:.1f}%"
