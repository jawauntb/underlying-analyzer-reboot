from __future__ import annotations

from app.alerts import build_alert_digest


def test_build_alert_digest_prioritizes_high_severity_rules() -> None:
    rows = [
        {
            "rank": 1,
            "ticker": "AAPL",
            "lane": "Priority",
            "score": 62.5,
            "setup": "BUY / long bias / above value",
            "annual_volatility": 0.28,
            "ridge": {"recommendation": "BUY"},
            "flow": {"fresh_long": True},
            "auction": {"location": "above value"},
        },
        {
            "rank": 2,
            "ticker": "MSFT",
            "lane": "Risk",
            "score": -24.0,
            "setup": "SELL / short bias / below value",
            "annual_volatility": 0.72,
            "ridge": {"recommendation": "SELL"},
            "flow": {"fresh_short": True},
            "auction": {"location": "below value"},
        },
    ]

    payload = build_alert_digest(rows, max_alerts=3, volatility_threshold=0.55)

    assert [alert["severity"] for alert in payload["alerts"]] == ["High", "High", "High"]
    assert payload["alerts"][0]["ticker"] == "AAPL"
    assert payload["digest"]["severity_counts"]["High"] == 3
    assert payload["digest"]["lane_counts"] == {"Priority": 1, "Risk": 1}
    assert payload["digest"]["priority_tickers"] == ["AAPL"]
    assert payload["digest"]["risk_tickers"] == ["MSFT"]


def test_build_alert_digest_returns_empty_queue_when_rules_do_not_fire() -> None:
    rows = [
        {
            "rank": 1,
            "ticker": "AAPL",
            "lane": "Review",
            "score": 8.0,
            "annual_volatility": 0.18,
            "ridge": {"recommendation": "WATCH"},
            "flow": {"fresh_long": False, "fresh_short": False},
            "auction": {"location": "inside value"},
        }
    ]

    payload = build_alert_digest(rows)

    assert payload["alerts"] == []
    assert payload["digest"]["headline"] == "No high-signal alerts across 1 names"
    assert payload["digest"]["summary"] == "No names crossed the alert rules in this run."
