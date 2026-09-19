"""Jev materiality scoring for the alerts feed.

Every alert in a digest gets one optional, additive ``jev_materiality`` field::

    {"level": "noise" | "minor" | "material" | "urgent",
     "score": <0..1, probability-weighted across the four levels>,
     "confidence": <0..1>}

Scoring is one batched ``systemone`` request per digest page (one ``score``
question per alert, all sharing one ``state``), never one HTTP call per
alert, and chunked at :data:`MAX_BATCH_ITEMS` so the shared state stays well
under Jev's context budget.

Results are memoized in-process for :data:`CACHE_TTL_SECONDS`, keyed by the
alert's stable id plus a hash of its user-visible content, so list polling
does not re-score identical alerts.

This module fails OPEN: any Jev error, timeout, missing ``JEV_API_KEY``, or
answer below the product-wide confidence floor simply leaves the field off
that alert. It never raises into a request, and
:func:`filter_alerts_by_materiality` always keeps unscored alerts.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from app.jev import JevClient, JevError, ScoreAnswer, parse_score_answer

MATERIALITY_LEVELS: tuple[str, ...] = ("noise", "minor", "material", "urgent")
MATERIALITY_FIELD = "jev_materiality"

#: Successful (confident or low-confidence) answers are reused for this long.
CACHE_TTL_SECONDS = 15 * 60
#: A failed Jev call is remembered briefly so a polling client does not hammer
#: an unavailable endpoint, but recovers quickly once Jev is back.
FAILURE_TTL_SECONDS = 60
#: One systemone request carries at most this many alerts.
MAX_BATCH_ITEMS = 50

_CONTENT_FIELDS: tuple[str, ...] = (
    "ticker",
    "severity",
    "category",
    "title",
    "message",
    "action",
)

_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_cache_lock = threading.Lock()


def clear_materiality_cache() -> None:
    """Drop every memoized score (tests and manual diagnostics only)."""
    with _cache_lock:
        _cache.clear()


def parse_min_materiality(value: Any) -> str | None:
    """Normalize a ``min_materiality`` query/body value to a known level, or ``None``."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return text if text in MATERIALITY_LEVELS else None


def score_alert_materiality(
    alerts: Sequence[dict[str, Any]],
    *,
    jev_client: Any | None = None,
    now: Callable[[], float] = time.monotonic,
) -> list[dict[str, Any]]:
    """Return shallow copies of ``alerts`` with ``jev_materiality`` where Jev was confident.

    Alerts Jev could not score (error, no key, low confidence) come back
    unchanged apart from the copy. The input list is never mutated and the
    output keeps the input order and length.
    """
    if not alerts:
        return []

    started = now()
    keys = [_cache_key(alert) for alert in alerts]
    results: list[dict[str, Any] | None] = [None] * len(alerts)
    pending: list[int] = []
    with _cache_lock:
        for index, key in enumerate(keys):
            hit = _cache.get(key)
            if hit is not None and hit[0] > started:
                results[index] = hit[1]
            else:
                pending.append(index)

    if pending:
        scored = _score_uncached(
            [alerts[index] for index in pending], jev_client=jev_client
        )
        with _cache_lock:
            for index, (value, ttl) in zip(pending, scored, strict=True):
                _cache[keys[index]] = (started + ttl, value)
                results[index] = value

    return [
        {**alert, MATERIALITY_FIELD: result} if result is not None else dict(alert)
        for alert, result in zip(alerts, results, strict=True)
    ]


def filter_alerts_by_materiality(
    alerts: Sequence[dict[str, Any]], min_level: str | None
) -> list[dict[str, Any]]:
    """Drop alerts whose *known* level is below ``min_level``; keep unscored ones."""
    threshold = parse_min_materiality(min_level)
    if threshold is None:
        return list(alerts)
    floor = MATERIALITY_LEVELS.index(threshold)
    kept: list[dict[str, Any]] = []
    for alert in alerts:
        materiality = alert.get(MATERIALITY_FIELD)
        level = materiality.get("level") if isinstance(materiality, dict) else None
        if level not in MATERIALITY_LEVELS or MATERIALITY_LEVELS.index(level) >= floor:
            kept.append(alert)
    return kept


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _content_hash(alert: dict[str, Any]) -> str:
    content = {name: alert.get(name) for name in _CONTENT_FIELDS}
    encoded = json.dumps(content, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()[:16]


def _cache_key(alert: dict[str, Any]) -> str:
    return f"{alert.get('id') or ''}:{_content_hash(alert)}"


def _score_uncached(
    alerts: list[dict[str, Any]], *, jev_client: Any | None
) -> list[tuple[dict[str, Any] | None, float]]:
    """Score ``alerts`` in chunks; each entry is ``(materiality_or_None, cache_ttl)``."""
    client = jev_client if jev_client is not None else JevClient()
    if jev_client is None and not getattr(client, "api_key", None):
        # No key configured: skip the network entirely and fail open.
        return [(None, FAILURE_TTL_SECONDS)] * len(alerts)

    out: list[tuple[dict[str, Any] | None, float]] = []
    for start in range(0, len(alerts), MAX_BATCH_ITEMS):
        chunk = alerts[start : start + MAX_BATCH_ITEMS]
        out.extend(_score_chunk(client, chunk))
    return out


def _score_chunk(
    client: Any, chunk: list[dict[str, Any]]
) -> list[tuple[dict[str, Any] | None, float]]:
    ids = [f"a{index}" for index in range(len(chunk))]
    state = {
        "alerts": [
            {"id": qid, **{name: alert.get(name) for name in _CONTENT_FIELDS}}
            for qid, alert in zip(ids, chunk, strict=True)
        ]
    }
    questions = {
        qid: {
            "type": "score",
            "instructions": (
                f"Consider only the alert with id '{qid}' in state.alerts. For an "
                "active investor watching this ticker, how material is it? "
                "noise = safe to ignore; minor = worth a glance; material = "
                "deserves attention today; urgent = act on it now."
            ),
            "criteria": list(MATERIALITY_LEVELS),
        }
        for qid in ids
    }
    try:
        answers = client.ask(state, questions)
    except JevError:
        return [(None, FAILURE_TTL_SECONDS)] * len(chunk)
    except Exception:  # noqa: BLE001 - defensive: scoring must never raise
        return [(None, FAILURE_TTL_SECONDS)] * len(chunk)

    scored: list[tuple[dict[str, Any] | None, float]] = []
    for qid in ids:
        try:
            answer = parse_score_answer(answers.get(qid))
        except JevError:
            scored.append((None, FAILURE_TTL_SECONDS))
            continue
        scored.append((materiality_from_answer(answer), CACHE_TTL_SECONDS))
    return scored


def materiality_from_answer(answer: ScoreAnswer) -> dict[str, Any] | None:
    """Turn a Jev score answer into the public ``jev_materiality`` object.

    Returns ``None`` below the confidence floor. The level is the most
    probable criterion when Jev returns per-level probabilities (falling back
    to the rounded score index otherwise), and ``score`` is the
    probability-weighted position on the 0..1 noise-to-urgent scale.
    """
    if not answer.is_confident:
        return None

    top = len(MATERIALITY_LEVELS) - 1
    probabilities = _level_probabilities(answer)
    if probabilities:
        total = sum(probabilities.values())
        if total <= 0:
            return None
        level = max(MATERIALITY_LEVELS, key=lambda name: probabilities.get(name, 0.0))
        weighted = sum(
            probabilities.get(name, 0.0) * (index / top)
            for index, name in enumerate(MATERIALITY_LEVELS)
        )
        score = weighted / total
    else:
        clamped = min(max(answer.score, 0.0), float(top))
        level = MATERIALITY_LEVELS[int(round(clamped))]
        score = clamped / top

    return {
        "level": level,
        "score": round(min(max(score, 0.0), 1.0), 4),
        "confidence": round(min(max(answer.confidence, 0.0), 1.0), 4),
    }


def _level_probabilities(answer: ScoreAnswer) -> dict[str, float]:
    """Map Jev's probabilities onto level names, tolerating index or legend keys."""
    raw = answer.probabilities or {}
    if not raw:
        return {}

    legend_to_level: dict[str, str] = {}
    for key, value in (answer.legend or {}).items():
        if isinstance(value, str) and value in MATERIALITY_LEVELS:
            legend_to_level[str(key)] = value
        elif isinstance(key, str) and key in MATERIALITY_LEVELS:
            legend_to_level[str(value)] = key

    out: dict[str, float] = {}
    for key, value in raw.items():
        try:
            probability = float(value)
        except (TypeError, ValueError):
            continue
        name = str(key)
        if name in MATERIALITY_LEVELS:
            level = name
        elif name in legend_to_level:
            level = legend_to_level[name]
        elif name.lstrip("-").isdigit() and 0 <= int(name) < len(MATERIALITY_LEVELS):
            level = MATERIALITY_LEVELS[int(name)]
        else:
            continue
        out[level] = out.get(level, 0.0) + max(probability, 0.0)
    return out
