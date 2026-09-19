"""Jev materiality scoring on the alerts feed.

Every test mocks the Jev HTTP layer (a stub client or a patched
``JevClient.ask``); nothing here needs a key or the network.
"""

from __future__ import annotations

from typing import Any

import pytest

from app import alert_materiality as materiality_module
from app.alert_materiality import (
    MATERIALITY_FIELD,
    MATERIALITY_LEVELS,
    clear_materiality_cache,
    filter_alerts_by_materiality,
    materiality_from_answer,
    parse_min_materiality,
    score_alert_materiality,
)
from app.alerts import build_alert_digest
from app.jev import JevClient, JevError, ScoreAnswer


class _StubJevClient:
    """Records ``ask`` calls and answers every question from one template."""

    def __init__(
        self,
        *,
        answer: dict[str, Any] | None = None,
        per_id: dict[str, dict[str, Any]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.answer = answer
        self.per_id = per_id or {}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def ask(self, state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        self.calls.append({"state": state, "questions": questions})
        if self.error is not None:
            raise self.error
        answers: dict[str, Any] = {}
        for qid in questions:
            answers[qid] = self.per_id.get(qid) or self.answer or _score_answer("material", 0.82)
        return answers


def _score_answer(level: str, confidence: float) -> dict[str, Any]:
    probabilities = dict.fromkeys(MATERIALITY_LEVELS, 0.03)
    probabilities[level] = 1.0 - 0.03 * (len(MATERIALITY_LEVELS) - 1)
    return {
        "type": "score",
        "score": float(MATERIALITY_LEVELS.index(level)),
        "legend": {str(i): name for i, name in enumerate(MATERIALITY_LEVELS)},
        "probabilities": probabilities,
        "confidence": confidence,
    }


def _alert(ticker: str, kind: str, **overrides: Any) -> dict[str, Any]:
    return {
        "id": f"{ticker.lower()}-{kind}",
        "ticker": ticker,
        "rank": 1,
        "lane": "Priority",
        "score": 50.0,
        "severity": "High",
        "category": "Setup",
        "title": kind.replace("-", " ").title(),
        "message": f"{ticker} fired {kind}.",
        "action": "Review.",
        **overrides,
    }


@pytest.fixture(autouse=True)
def _fresh_cache() -> Any:
    clear_materiality_cache()
    yield
    clear_materiality_cache()


# ---------------------------------------------------------------------------
# Shape and batching
# ---------------------------------------------------------------------------


def test_scores_every_alert_in_one_batched_request() -> None:
    stub = _StubJevClient()
    alerts = [_alert("AAPL", "priority-setup"), _alert("MSFT", "risk-lane")]

    scored = score_alert_materiality(alerts, jev_client=stub)

    assert len(stub.calls) == 1, "one systemone request per page, never one per alert"
    call = stub.calls[0]
    assert [item["id"] for item in call["state"]["alerts"]] == ["a0", "a1"]
    assert call["state"]["alerts"][0]["title"] == "Priority Setup"
    assert set(call["questions"]) == {"a0", "a1"}
    for question in call["questions"].values():
        assert question["type"] == "score"
        assert question["criteria"] == list(MATERIALITY_LEVELS)

    assert [alert["id"] for alert in scored] == [alert["id"] for alert in alerts]
    for alert in scored:
        materiality = alert[MATERIALITY_FIELD]
        assert materiality["level"] == "material"
        assert 0.0 <= materiality["score"] <= 1.0
        assert materiality["confidence"] == 0.82
    # The input list is untouched.
    assert MATERIALITY_FIELD not in alerts[0]


def test_chunks_pages_larger_than_the_batch_cap() -> None:
    stub = _StubJevClient()
    alerts = [_alert("T" + str(i), "priority-setup") for i in range(120)]

    scored = score_alert_materiality(alerts, jev_client=stub)

    assert len(stub.calls) == 3
    assert [len(call["questions"]) for call in stub.calls] == [50, 50, 20]
    assert all(MATERIALITY_FIELD in alert for alert in scored)


def test_score_is_probability_weighted_and_level_is_argmax() -> None:
    answer = ScoreAnswer(
        score=2.0,
        legend={},
        probabilities={"noise": 0.0, "minor": 0.2, "material": 0.6, "urgent": 0.2},
        confidence=0.9,
    )
    materiality = materiality_from_answer(answer)
    assert materiality == {
        "level": "material",
        "score": pytest.approx((0.2 * 1 + 0.6 * 2 + 0.2 * 3) / 3, abs=1e-4),
        "confidence": 0.9,
    }


def test_index_keyed_probabilities_resolve_through_the_legend() -> None:
    answer = ScoreAnswer(
        score=3.0,
        legend={"0": "noise", "1": "minor", "2": "material", "3": "urgent"},
        probabilities={"0": 0.05, "1": 0.05, "2": 0.1, "3": 0.8},
        confidence=0.7,
    )
    materiality = materiality_from_answer(answer)
    assert materiality is not None
    assert materiality["level"] == "urgent"
    assert materiality["score"] > 0.85


def test_falls_back_to_the_rounded_score_without_probabilities() -> None:
    answer = ScoreAnswer(score=1.2, legend={}, probabilities={}, confidence=0.6)
    materiality = materiality_from_answer(answer)
    assert materiality == {"level": "minor", "score": 0.4, "confidence": 0.6}


# ---------------------------------------------------------------------------
# Fail open
# ---------------------------------------------------------------------------


def test_low_confidence_answers_are_omitted() -> None:
    stub = _StubJevClient(answer=_score_answer("urgent", 0.4))
    scored = score_alert_materiality([_alert("AAPL", "priority-setup")], jev_client=stub)
    assert MATERIALITY_FIELD not in scored[0]
    assert scored[0]["title"] == "Priority Setup"


def test_jev_error_leaves_alerts_unchanged() -> None:
    stub = _StubJevClient(error=JevError("boom"))
    alerts = [_alert("AAPL", "priority-setup")]
    scored = score_alert_materiality(alerts, jev_client=stub)
    assert scored == alerts
    assert scored[0] is not alerts[0]


def test_unexpected_exception_leaves_alerts_unchanged() -> None:
    stub = _StubJevClient(error=RuntimeError("network stack exploded"))
    alerts = [_alert("AAPL", "priority-setup")]
    assert score_alert_materiality(alerts, jev_client=stub) == alerts


def test_missing_key_skips_the_network_and_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JEV_API_KEY", raising=False)

    def _never(*_: Any, **__: Any) -> Any:
        raise AssertionError("Jev must not be called without a key")

    monkeypatch.setattr(JevClient, "ask", _never)
    alerts = [_alert("AAPL", "priority-setup")]
    assert score_alert_materiality(alerts) == alerts


def test_malformed_answer_for_one_alert_only_drops_that_alert() -> None:
    stub = _StubJevClient(per_id={"a1": {"type": "score", "score": "not a number"}})
    scored = score_alert_materiality(
        [_alert("AAPL", "priority-setup"), _alert("MSFT", "risk-lane")], jev_client=stub
    )
    assert MATERIALITY_FIELD in scored[0]
    assert MATERIALITY_FIELD not in scored[1]


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_second_call_with_identical_alerts_does_not_re_score() -> None:
    stub = _StubJevClient()
    alerts = [_alert("AAPL", "priority-setup"), _alert("MSFT", "risk-lane")]

    first = score_alert_materiality(alerts, jev_client=stub)
    second = score_alert_materiality(alerts, jev_client=stub)

    assert len(stub.calls) == 1
    assert second == first


def test_changed_content_re_scores_only_the_changed_alert() -> None:
    stub = _StubJevClient()
    alerts = [_alert("AAPL", "priority-setup"), _alert("MSFT", "risk-lane")]
    score_alert_materiality(alerts, jev_client=stub)

    changed = [alerts[0], {**alerts[1], "message": "MSFT message changed."}]
    score_alert_materiality(changed, jev_client=stub)

    assert len(stub.calls) == 2
    assert [item["ticker"] for item in stub.calls[1]["state"]["alerts"]] == ["MSFT"]


def test_cache_entries_expire() -> None:
    clock = {"now": 1000.0}
    stub = _StubJevClient()
    alerts = [_alert("AAPL", "priority-setup")]

    score_alert_materiality(alerts, jev_client=stub, now=lambda: clock["now"])
    clock["now"] += materiality_module.CACHE_TTL_SECONDS + 1
    score_alert_materiality(alerts, jev_client=stub, now=lambda: clock["now"])

    assert len(stub.calls) == 2


def test_failures_are_retried_after_the_short_failure_ttl() -> None:
    clock = {"now": 1000.0}
    stub = _StubJevClient(error=JevError("boom"))
    alerts = [_alert("AAPL", "priority-setup")]

    score_alert_materiality(alerts, jev_client=stub, now=lambda: clock["now"])
    score_alert_materiality(alerts, jev_client=stub, now=lambda: clock["now"])
    assert len(stub.calls) == 1, "a failed batch is not retried on the very next poll"

    clock["now"] += materiality_module.FAILURE_TTL_SECONDS + 1
    stub.error = None
    scored = score_alert_materiality(alerts, jev_client=stub, now=lambda: clock["now"])
    assert len(stub.calls) == 2
    assert MATERIALITY_FIELD in scored[0]


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


def test_filter_drops_known_low_levels_and_keeps_unscored() -> None:
    alerts = [
        {"id": "a", MATERIALITY_FIELD: {"level": "noise", "score": 0.0, "confidence": 0.9}},
        {"id": "b", MATERIALITY_FIELD: {"level": "material", "score": 0.7, "confidence": 0.9}},
        {"id": "c"},
        {"id": "d", MATERIALITY_FIELD: {"level": "urgent", "score": 1.0, "confidence": 0.9}},
        {"id": "e", MATERIALITY_FIELD: {"level": "minor", "score": 0.3, "confidence": 0.9}},
    ]
    kept = filter_alerts_by_materiality(alerts, "material")
    assert [alert["id"] for alert in kept] == ["b", "c", "d"]


def test_filter_without_a_floor_is_a_no_op() -> None:
    alerts = [{"id": "a", MATERIALITY_FIELD: {"level": "noise"}}, {"id": "b"}]
    assert filter_alerts_by_materiality(alerts, None) == alerts
    assert filter_alerts_by_materiality(alerts, "nonsense") == alerts


def test_parse_min_materiality_normalizes_and_rejects_unknown_values() -> None:
    assert parse_min_materiality(" Material ") == "material"
    assert parse_min_materiality("URGENT") == "urgent"
    assert parse_min_materiality("high") is None
    assert parse_min_materiality(None) is None
    assert parse_min_materiality("") is None


# ---------------------------------------------------------------------------
# Digest integration
# ---------------------------------------------------------------------------


def _rows() -> list[dict[str, Any]]:
    return [
        {
            "rank": 1,
            "ticker": "AAPL",
            "lane": "Priority",
            "score": 62.5,
            "setup": "BUY / long bias",
            "annual_volatility": 0.28,
            "ridge": {"recommendation": "BUY"},
            "flow": {"fresh_long": True},
            "auction": {"location": "inside value"},
        },
        {
            "rank": 2,
            "ticker": "MSFT",
            "lane": "Risk",
            "score": -24.0,
            "annual_volatility": 0.2,
            "ridge": {"recommendation": "WATCH"},
            "flow": {},
            "auction": {},
        },
    ]


def test_digest_scores_the_capped_page_and_filters_by_floor() -> None:
    def scorer(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for alert in alerts:
            level = "urgent" if alert["ticker"] == "MSFT" else "minor"
            out.append(
                {**alert, MATERIALITY_FIELD: {"level": level, "score": 0.5, "confidence": 0.9}}
            )
        return out

    payload = build_alert_digest(
        _rows(), max_alerts=10, materiality_scorer=scorer, min_materiality="material"
    )

    assert [alert["ticker"] for alert in payload["alerts"]] == ["MSFT"]
    assert payload["alerts"][0][MATERIALITY_FIELD]["level"] == "urgent"
    # The digest describes exactly the alerts returned.
    assert payload["digest"]["severity_counts"] == {"High": 1}


def test_digest_without_a_scorer_is_unchanged() -> None:
    payload = build_alert_digest(_rows(), max_alerts=10)
    assert payload["alerts"]
    assert all(MATERIALITY_FIELD not in alert for alert in payload["alerts"])
