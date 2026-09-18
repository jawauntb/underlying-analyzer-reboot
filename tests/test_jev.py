from __future__ import annotations

from typing import Any, cast

import pytest
import requests

from app.jev import (
    JEV_API_URL,
    JEV_LOW_CONFIDENCE_THRESHOLD,
    JevClient,
    JevError,
    parse_choice_answer,
    parse_noul_answer,
    parse_score_answer,
)


class FakeJevResponse:
    """A minimal stand-in for ``requests.Response`` (status + json + text)."""

    def __init__(self, status_code: int, body: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeJevSession:
    """Replays a scripted sequence of responses, or raises on ``post``."""

    def __init__(self, responses: list[FakeJevResponse] | None = None) -> None:
        self.responses = responses or []
        self.calls: list[dict[str, Any]] = []
        self.raise_on_call: Exception | None = None

    def post(self, url: str, **kwargs: Any) -> FakeJevResponse:
        self.calls.append({"url": url, **kwargs})
        if self.raise_on_call is not None:
            raise self.raise_on_call
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


def _client(session: FakeJevSession, **kwargs: Any) -> JevClient:
    return JevClient(api_key="jev-test-key", session=cast(requests.Session, session), **kwargs)


# ---------------------------------------------------------------------------
# Missing key fails closed
# ---------------------------------------------------------------------------


def test_ask_without_api_key_raises_jev_error() -> None:
    client = JevClient(api_key="", session=cast(requests.Session, FakeJevSession()))

    with pytest.raises(JevError, match="JEV_API_KEY"):
        client.ask("state", {"q": {"type": "noul", "instructions": "?"}})


def test_ask_without_api_key_never_hits_the_network() -> None:
    session = FakeJevSession()
    client = JevClient(api_key=None, session=cast(requests.Session, session))
    # Simulate no key anywhere in the environment for this client instance.
    client.api_key = None

    with pytest.raises(JevError):
        client.ask("state", {"q": {"type": "noul", "instructions": "?"}})

    assert session.calls == []


# ---------------------------------------------------------------------------
# Happy paths: request shape + answer parsing
# ---------------------------------------------------------------------------


def test_choice_sends_expected_request_and_parses_answer() -> None:
    session = FakeJevSession(
        [
            FakeJevResponse(
                200,
                {
                    "model": "jev-latest",
                    "answers": {
                        "q": {
                            "type": "choice",
                            "choice": "research",
                            "probabilities": {"research": 0.9, "charts": 0.1},
                            "confidence": 0.9,
                        }
                    },
                },
            )
        ]
    )
    client = _client(session)

    answer = client.choice(
        "Which group?", {"research": "...", "charts": "..."}, state="what is AAPL doing"
    )

    assert answer.choice == "research"
    assert answer.confidence == 0.9
    assert answer.is_confident

    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == JEV_API_URL
    assert call["headers"]["Authorization"] == "Bearer jev-test-key"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["json"]["model"] == "jev-latest"
    assert call["json"]["state"] == "what is AAPL doing"
    assert call["json"]["questions"]["q"]["type"] == "choice"
    assert call["json"]["questions"]["q"]["criteria"] == {"research": "...", "charts": "..."}


def test_noul_parses_answer() -> None:
    session = FakeJevSession(
        [FakeJevResponse(200, {"answers": {"q": {"type": "noul", "noul": 0.73}}})]
    )
    client = _client(session)

    answer = client.noul("Is this bullish?", state="some context")

    assert answer.noul == pytest.approx(0.73)


def test_score_parses_answer() -> None:
    session = FakeJevSession(
        [
            FakeJevResponse(
                200,
                {
                    "answers": {
                        "q": {
                            "type": "score",
                            "score": 2,
                            "legend": {"0": "low", "1": "mid", "2": "high"},
                            "probabilities": {"0": 0.05, "1": 0.15, "2": 0.8},
                            "confidence": 0.8,
                        }
                    }
                },
            )
        ]
    )
    client = _client(session)

    answer = client.score("Rate this.", ["low", "mid", "high"], state="context")

    assert answer.score == 2
    assert answer.confidence == 0.8
    assert answer.is_confident


def test_ask_batches_multiple_questions_in_one_call() -> None:
    session = FakeJevSession(
        [
            FakeJevResponse(
                200,
                {
                    "answers": {
                        "a": {"type": "noul", "noul": 0.1},
                        "b": {"type": "choice", "choice": "x", "confidence": 0.9},
                    }
                },
            )
        ]
    )
    client = _client(session)

    answers = client.ask(
        "state",
        {
            "a": {"type": "noul", "instructions": "?"},
            "b": {"type": "choice", "instructions": "?", "criteria": {"x": "", "y": ""}},
        },
    )

    assert len(session.calls) == 1
    assert set(answers) == {"a", "b"}


# ---------------------------------------------------------------------------
# Low confidence
# ---------------------------------------------------------------------------


def test_choice_answer_below_threshold_is_not_confident() -> None:
    session = FakeJevSession(
        [
            FakeJevResponse(
                200,
                {
                    "answers": {
                        "q": {
                            "type": "choice",
                            "choice": "research",
                            "confidence": JEV_LOW_CONFIDENCE_THRESHOLD - 0.01,
                        }
                    }
                },
            )
        ]
    )
    client = _client(session)

    answer = client.choice("Which?", {"research": ""}, state="x")

    assert not answer.is_confident


# ---------------------------------------------------------------------------
# Errors: validation, auth, malformed body
# ---------------------------------------------------------------------------


def test_422_raises_immediately_without_retry() -> None:
    session = FakeJevSession([FakeJevResponse(422, {"error": "bad question"})])
    client = _client(session)

    with pytest.raises(JevError, match="422"):
        client.ask("state", {"q": {"type": "noul", "instructions": "?"}})

    assert len(session.calls) == 1


def test_401_raises_immediately() -> None:
    session = FakeJevSession([FakeJevResponse(401, {"error": "bad key"})])
    client = _client(session)

    with pytest.raises(JevError, match="401"):
        client.ask("state", {"q": {"type": "noul", "instructions": "?"}})

    assert len(session.calls) == 1


def test_network_exception_raises_jev_error_without_retry() -> None:
    session = FakeJevSession()
    session.raise_on_call = requests.exceptions.ConnectionError("boom")
    client = _client(session)

    with pytest.raises(JevError, match="boom"):
        client.ask("state", {"q": {"type": "noul", "instructions": "?"}})


def test_malformed_json_body_raises_jev_error() -> None:
    session = FakeJevSession([FakeJevResponse(200, None)])
    client = _client(session)

    with pytest.raises(JevError):
        client.ask("state", {"q": {"type": "noul", "instructions": "?"}})


def test_missing_answer_key_raises_jev_error() -> None:
    session = FakeJevSession([FakeJevResponse(200, {"answers": {}})])
    client = _client(session)

    with pytest.raises(JevError, match="missing answer"):
        client.ask("state", {"q": {"type": "noul", "instructions": "?"}})


def test_unexpected_noul_shape_raises_jev_error() -> None:
    session = FakeJevSession([FakeJevResponse(200, {"answers": {"q": {"type": "choice"}}})])
    client = _client(session)

    with pytest.raises(JevError):
        client.noul("?", state="x")


# ---------------------------------------------------------------------------
# Retry-with-backoff on 429 / 529
# ---------------------------------------------------------------------------


def test_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("app.jev.time.sleep", lambda seconds: sleeps.append(seconds))

    session = FakeJevSession(
        [
            FakeJevResponse(429, {"error": "rate limited"}),
            FakeJevResponse(429, {"error": "rate limited"}),
            FakeJevResponse(200, {"answers": {"q": {"type": "noul", "noul": 0.5}}}),
        ]
    )
    client = _client(session, max_retries=3)

    answer = client.noul("?", state="x")

    assert answer.noul == 0.5
    assert len(session.calls) == 3
    # Never retries immediately: every retry sleeps for a positive duration.
    assert len(sleeps) == 2
    assert all(seconds > 0 for seconds in sleeps)


def test_retries_on_529_then_raises_after_exhausting_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.jev.time.sleep", lambda _seconds: None)

    session = FakeJevSession([FakeJevResponse(529, {"error": "overloaded"})] * 5)
    client = _client(session, max_retries=2)

    with pytest.raises(JevError, match="529"):
        client.ask("state", {"q": {"type": "noul", "instructions": "?"}})

    # Initial attempt + 2 retries = 3 calls total.
    assert len(session.calls) == 3


def test_zero_retries_fails_fast_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.jev.time.sleep", lambda _seconds: None)
    session = FakeJevSession([FakeJevResponse(429, {"error": "rate limited"})])
    client = _client(session, max_retries=0)

    with pytest.raises(JevError, match="429"):
        client.ask("state", {"q": {"type": "noul", "instructions": "?"}})

    assert len(session.calls) == 1


# ---------------------------------------------------------------------------
# Standalone parse_* helpers (used directly by batch callers like the scan script)
# ---------------------------------------------------------------------------


def test_parse_choice_answer_rejects_wrong_shape() -> None:
    with pytest.raises(JevError):
        parse_choice_answer({"type": "noul", "noul": 0.5})


def test_parse_score_answer_rejects_wrong_shape() -> None:
    with pytest.raises(JevError):
        parse_score_answer({"type": "choice", "choice": "x"})


def test_parse_noul_answer_rejects_non_numeric() -> None:
    with pytest.raises(JevError):
        parse_noul_answer({"type": "noul", "noul": "not-a-number"})
