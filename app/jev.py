"""Native client for Jev (TypeSafe's ``systemone`` endpoint).

Jev is a "System 1" model: fast (roughly 70-500ms) and effectively free
($0.042/M input tokens, $0 output), tuned for calibrated yes/no, single-choice,
and rubric-score decisions over a bounded (32k) context. It is deliberately
NOT used here for open-ended prose, multi-step reasoning, or math - callers
must never route real reasoning or writing work to it.

This client mirrors the shape of :class:`app.anthropic.AnthropicTextClient`:
it reads its key from the environment, builds one pooled HTTP session, and
turns every failure into :class:`JevError` instead of crashing. Every call
site in this codebase is expected to catch :class:`JevError` (and, for
choice/score answers, check ``is_confident``) and fall back to its existing,
pre-Jev behavior - Jev is an optimization, never a dependency the product can
fail on.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from app._perf import tune_session

JEV_API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_JEV_MODEL = "jev-latest"

#: Below this confidence, a choice/score answer must be treated as if Jev had
#: errored: fall back to existing behavior rather than trust the answer.
JEV_LOW_CONFIDENCE_THRESHOLD = 0.55

_RETRYABLE_STATUSES = frozenset({429, 529})
_DEFAULT_MAX_RETRIES = 3
_BASE_BACKOFF_SECONDS = 0.5
_MAX_BACKOFF_SECONDS = 8.0


class JevError(Exception):
    """Raised when a Jev call cannot complete.

    Always catch this at the call site and take the safe fallback path
    (call the original model, or skip the optimization) - never let it
    propagate into a request failure.
    """


@dataclass(frozen=True)
class NoulAnswer:
    """A yes/no probability answer."""

    noul: float


@dataclass(frozen=True)
class ChoiceAnswer:
    """A categorical answer."""

    choice: str
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0

    @property
    def is_confident(self) -> bool:
        return self.confidence >= JEV_LOW_CONFIDENCE_THRESHOLD


@dataclass(frozen=True)
class ScoreAnswer:
    """A rubric/ordered-score answer."""

    score: float
    legend: dict[str, Any] = field(default_factory=dict)
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0

    @property
    def is_confident(self) -> bool:
        return self.confidence >= JEV_LOW_CONFIDENCE_THRESHOLD


class JevClient:
    """Thin client for ``POST /v1/systemone``."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        session: requests.Session | None = None,
        timeout: float = 5.0,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("JEV_API_KEY")
        self.model = model if model is not None else os.getenv("JEV_MODEL") or DEFAULT_JEV_MODEL
        # Tune only sessions we create; an injected session is the caller's
        # (tests pass a lightweight fake with no real adapter mount point).
        self.session = session if session is not None else tune_session(requests.Session())
        self.timeout = timeout
        self.max_retries = max(0, max_retries)

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key or ''}",
            "Content-Type": "application/json",
        }

    # -- low level -----------------------------------------------------

    def ask(
        self,
        state: str | dict[str, Any] | list[Any],
        questions: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Send one ``systemone`` request with one or more questions.

        Returns the raw ``answers`` mapping (question id -> answer dict), so
        callers can batch several questions into a single request. Raises
        :class:`JevError` on any failure; never returns a partial/invalid
        result silently.
        """
        if not self.api_key:
            raise JevError("JEV_API_KEY is not configured")
        if not questions:
            raise JevError("Jev.ask requires at least one question")

        payload = {"state": state, "model": self.model, "questions": questions}

        response: requests.Response | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.post(
                    JEV_API_URL,
                    headers=self.headers(),
                    json=payload,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise JevError(f"Jev request failed: {exc}") from exc

            if response.status_code < 400:
                break

            if response.status_code in _RETRYABLE_STATUSES and attempt < self.max_retries:
                time.sleep(_backoff_seconds(attempt))
                continue

            raise JevError(
                f"Jev request failed with status {response.status_code}: "
                f"{_error_text(response)}"
            )

        assert response is not None  # loop always assigns or raises
        try:
            body = response.json()
        except ValueError as exc:
            raise JevError(f"Jev response was not valid JSON: {exc}") from exc

        answers = body.get("answers") if isinstance(body, dict) else None
        if not isinstance(answers, dict):
            raise JevError(f"Jev response missing 'answers': {body!r}")

        missing = [qid for qid in questions if qid not in answers]
        if missing:
            raise JevError(f"Jev response missing answer(s) for: {', '.join(missing)}")

        return answers

    # -- convenience: single-question calls -----------------------------

    def noul(
        self,
        instructions: str,
        *,
        state: str | dict[str, Any] | list[Any],
        question_id: str = "q",
    ) -> NoulAnswer:
        """Ask a single calibrated yes/no probability question."""
        questions = {question_id: {"type": "noul", "instructions": instructions}}
        answers = self.ask(state, questions)
        return parse_noul_answer(answers[question_id])

    def choice(
        self,
        instructions: str,
        criteria: dict[str, Any],
        *,
        state: str | dict[str, Any] | list[Any],
        question_id: str = "q",
    ) -> ChoiceAnswer:
        """Ask a single categorical question over ``criteria``'s keys."""
        questions = {
            question_id: {
                "type": "choice",
                "instructions": instructions,
                "criteria": criteria,
            }
        }
        answers = self.ask(state, questions)
        return parse_choice_answer(answers[question_id])

    def score(
        self,
        instructions: str,
        criteria: list[str],
        *,
        state: str | dict[str, Any] | list[Any],
        question_id: str = "q",
    ) -> ScoreAnswer:
        """Ask a single ordered-rubric question."""
        questions = {
            question_id: {
                "type": "score",
                "instructions": instructions,
                "criteria": criteria,
            }
        }
        answers = self.ask(state, questions)
        return parse_score_answer(answers[question_id])


# ---------------------------------------------------------------------------
# Answer parsing (shared by the convenience methods and batched callers)
# ---------------------------------------------------------------------------


def parse_noul_answer(raw: Any) -> NoulAnswer:
    if not isinstance(raw, dict) or raw.get("type") != "noul":
        raise JevError(f"Jev returned an unexpected noul answer: {raw!r}")
    try:
        value = float(raw["noul"])
    except (KeyError, TypeError, ValueError) as exc:
        raise JevError(f"Jev noul answer was not numeric: {raw!r}") from exc
    return NoulAnswer(noul=value)


def parse_choice_answer(raw: Any) -> ChoiceAnswer:
    if not isinstance(raw, dict) or raw.get("type") != "choice" or "choice" not in raw:
        raise JevError(f"Jev returned an unexpected choice answer: {raw!r}")
    probabilities = raw.get("probabilities")
    if not isinstance(probabilities, dict):
        probabilities = {}
    return ChoiceAnswer(
        choice=str(raw["choice"]),
        probabilities=probabilities,
        confidence=_safe_float(raw.get("confidence")),
    )


def parse_score_answer(raw: Any) -> ScoreAnswer:
    if not isinstance(raw, dict) or raw.get("type") != "score" or "score" not in raw:
        raise JevError(f"Jev returned an unexpected score answer: {raw!r}")
    try:
        score_value = float(raw["score"])
    except (TypeError, ValueError) as exc:
        raise JevError(f"Jev score answer was not numeric: {raw!r}") from exc
    legend = raw.get("legend")
    probabilities = raw.get("probabilities")
    return ScoreAnswer(
        score=score_value,
        legend=legend if isinstance(legend, dict) else {},
        probabilities=probabilities if isinstance(probabilities, dict) else {},
        confidence=_safe_float(raw.get("confidence")),
    )


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with jitter; never retries immediately."""
    base = _BASE_BACKOFF_SECONDS * (2**attempt)
    capped = min(base, _MAX_BACKOFF_SECONDS)
    return capped + random.uniform(0, capped * 0.25)


def _error_text(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text or "Jev request failed"
    if isinstance(payload, dict):
        error = payload.get("error") or payload.get("message")
        if error:
            return str(error)
    return "Jev request failed"
