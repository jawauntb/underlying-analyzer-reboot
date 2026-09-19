"""Batched citation-type classification: the library, the endpoint, and memo annotations.

Every Jev interaction here is a stub or a patched ``JevClient.ask``; nothing
needs ``JEV_API_KEY`` or the network.
"""

from __future__ import annotations

from typing import Any

import pytest

from app import citation_verify as citation_module
from app.citation_verify import classify_citation_regex, classify_citations
from app.jev import JevClient, JevError
from app.main import create_app
from app.memo_citations import (
    CITATION_TYPE_FIELD,
    annotate_citations,
    annotate_packet_citations,
    citation_text,
)

XBRL = "(SEC XBRL Revenue, Q1 FY2027: $137,237M)"
FILING = "(SEC 10-K Item 1 Business, filed 2026-02-25)"
EXA = "(Exa: techcrunch.com, 2025-12-03)"
ODD_ONE = "(Bloomberg terminal snapshot, 2026-05-01)"
ODD_TWO = "(Company investor day slides)"


class _StubJevAskClient:
    """Answers batched ``choice`` questions; records every ``ask`` call."""

    def __init__(
        self,
        *,
        choice: str = "exa",
        confidence: float = 0.8,
        per_id: dict[str, dict[str, Any]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.choice = choice
        self.confidence = confidence
        self.per_id = per_id or {}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def ask(self, state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        self.calls.append({"state": state, "questions": questions})
        if self.error is not None:
            raise self.error
        return {
            qid: self.per_id.get(qid)
            or {"type": "choice", "choice": self.choice, "confidence": self.confidence}
            for qid in questions
        }


@pytest.fixture(autouse=True)
def _fresh_cache() -> Any:
    citation_module.clear_classify_cache()
    yield
    citation_module.clear_classify_cache()


# ---------------------------------------------------------------------------
# classify_citations
# ---------------------------------------------------------------------------


def test_regex_matches_are_attributed_to_regex_and_never_sent_to_jev() -> None:
    stub = _StubJevAskClient(error=AssertionError("Jev must not be called"))

    results = classify_citations([XBRL, FILING, EXA], jev_client=stub)

    assert results == [
        {"citation": XBRL, "type": "sec_xbrl", "source": "regex", "confidence": None},
        {"citation": FILING, "type": "sec_filing", "source": "regex", "confidence": None},
        {"citation": EXA, "type": "exa", "source": "regex", "confidence": None},
    ]
    assert stub.calls == []


def test_unmatched_citations_go_to_jev_in_one_batched_request() -> None:
    stub = _StubJevAskClient(
        per_id={
            "c0": {"type": "choice", "choice": "exa", "confidence": 0.71},
            "c1": {"type": "choice", "choice": "sec_filing", "confidence": 0.66},
        }
    )

    results = classify_citations([XBRL, ODD_ONE, ODD_TWO], jev_client=stub)

    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["state"] == {
        "citations": [{"id": "c0", "citation": ODD_ONE}, {"id": "c1", "citation": ODD_TWO}]
    }
    assert set(call["questions"]) == {"c0", "c1"}
    assert all(q["type"] == "choice" for q in call["questions"].values())
    assert results[0]["source"] == "regex"
    assert results[1] == {"citation": ODD_ONE, "type": "exa", "source": "jev", "confidence": 0.71}
    assert results[2] == {
        "citation": ODD_TWO,
        "type": "sec_filing",
        "source": "jev",
        "confidence": 0.66,
    }


def test_low_confidence_and_unknown_jev_answers_stay_unknown() -> None:
    stub = _StubJevAskClient(
        per_id={
            "c0": {"type": "choice", "choice": "exa", "confidence": 0.3},
            "c1": {"type": "choice", "choice": "unknown", "confidence": 0.95},
        }
    )
    results = classify_citations([ODD_ONE, ODD_TWO], jev_client=stub)
    assert results == [
        {"citation": ODD_ONE, "type": "unknown", "source": "regex", "confidence": None},
        {"citation": ODD_TWO, "type": "unknown", "source": "regex", "confidence": None},
    ]


def test_jev_error_fails_open_to_unknown() -> None:
    stub = _StubJevAskClient(error=JevError("boom"))
    results = classify_citations([XBRL, ODD_ONE], jev_client=stub)
    assert results[0]["type"] == "sec_xbrl"
    assert results[1] == {
        "citation": ODD_ONE,
        "type": "unknown",
        "source": "regex",
        "confidence": None,
    }


def test_missing_key_never_touches_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JEV_API_KEY", raising=False)

    def _never(*_: Any, **__: Any) -> Any:
        raise AssertionError("Jev must not be called without a key")

    monkeypatch.setattr(JevClient, "ask", _never)
    results = classify_citations([ODD_ONE])
    assert results[0]["type"] == "unknown"


def test_jev_verdicts_are_cached_across_calls() -> None:
    stub = _StubJevAskClient(choice="exa", confidence=0.9)

    first = classify_citations([ODD_ONE, ODD_TWO], jev_client=stub)
    second = classify_citations([ODD_TWO, ODD_ONE], jev_client=stub)

    assert len(stub.calls) == 1
    assert second == [first[1], first[0]]


def test_batches_are_chunked_at_fifty() -> None:
    stub = _StubJevAskClient()
    citations = [f"(Odd citation number {index})" for index in range(70)]
    results = classify_citations(citations, jev_client=stub)
    assert [len(call["questions"]) for call in stub.calls] == [50, 20]
    assert len(results) == 70


def test_regex_helper_returns_none_on_a_miss() -> None:
    assert classify_citation_regex(ODD_ONE) is None
    assert classify_citation_regex(XBRL) == {
        "kind": "sec_xbrl",
        "target": "Revenue",
        "period": "Q1 FY2027",
        "value": "$137,237M",
    }


# ---------------------------------------------------------------------------
# POST /api/citations/classify
# ---------------------------------------------------------------------------


def test_classify_endpoint_shape_and_source_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    calls: list[dict[str, Any]] = []

    def fake_ask(_self: JevClient, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        calls.append({"state": state, "questions": questions})
        return {qid: {"type": "choice", "choice": "exa", "confidence": 0.71} for qid in questions}

    monkeypatch.setattr(JevClient, "ask", fake_ask)
    client = create_app().test_client()

    response = client.post(
        "/api/citations/classify", json={"citations": [XBRL, ODD_ONE, ODD_TWO]}
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "results": [
            {"citation": XBRL, "type": "sec_xbrl", "source": "regex", "confidence": None},
            {"citation": ODD_ONE, "type": "exa", "source": "jev", "confidence": 0.71},
            {"citation": ODD_TWO, "type": "exa", "source": "jev", "confidence": 0.71},
        ]
    }
    assert len(calls) == 1, "the endpoint batches every unmatched citation into one call"


def test_classify_endpoint_fails_open_when_jev_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEV_API_KEY", "test-key")

    def broken_ask(_self: JevClient, _state: Any, _questions: dict[str, Any]) -> dict[str, Any]:
        raise JevError("529 overloaded")

    monkeypatch.setattr(JevClient, "ask", broken_ask)
    client = create_app().test_client()

    response = client.post("/api/citations/classify", json={"citations": [ODD_ONE, EXA]})

    assert response.status_code == 200
    assert response.get_json()["results"] == [
        {"citation": ODD_ONE, "type": "unknown", "source": "regex", "confidence": None},
        {"citation": EXA, "type": "exa", "source": "regex", "confidence": None},
    ]


def test_classify_endpoint_validates_its_body() -> None:
    client = create_app().test_client()
    assert client.post("/api/citations/classify", json={}).status_code == 400
    assert client.post("/api/citations/classify", json={"citations": "x"}).status_code == 400
    assert client.post("/api/citations/classify", json={"citations": [1]}).status_code == 400
    assert client.post("/api/citations/classify", json=[XBRL]).status_code == 400
    too_many = client.post("/api/citations/classify", json={"citations": ["(x: 1)"] * 201})
    assert too_many.status_code == 400
    empty = client.post("/api/citations/classify", json={"citations": []})
    assert empty.status_code == 200
    assert empty.get_json() == {"results": []}


def test_classify_endpoint_is_in_the_catalog_and_openapi() -> None:
    client = create_app().test_client()
    docs = client.get("/api/docs").get_json()
    assert any(e["path"] == "/api/citations/classify" for e in docs["endpoints"])
    openapi = client.get("/api/openapi").get_json()
    operation = openapi["paths"]["/api/citations/classify"]["post"]
    assert operation["requestBody"]["required"] is True
    assert "results" in operation["responses"]["200"]["content"]["application/json"]["schema"][
        "properties"
    ]


# ---------------------------------------------------------------------------
# Memo citation annotations
# ---------------------------------------------------------------------------


def test_citation_text_mirrors_the_rendered_row() -> None:
    row = {"id": "C1", "claim": "10-K filed 2026-02-25", "source": "SEC EDGAR", "url": "https://x"}
    assert citation_text(row) == "(SEC EDGAR: 10-K filed 2026-02-25, https://x)"
    bare = {"claim": "Regime is bull", "source": "", "url": None}
    assert citation_text(bare) == "(Regime is bull)"


def test_annotate_citations_adds_the_field_only_for_known_types() -> None:
    stub = _StubJevAskClient(
        per_id={
            "c0": {"type": "choice", "choice": "sec_filing", "confidence": 0.78},
            "c1": {"type": "choice", "choice": "unknown", "confidence": 0.9},
        }
    )
    citations = [
        {"id": "C1", "claim": "10-K filed 2026-02-25", "source": "SEC EDGAR", "url": None},
        {"id": "C2", "claim": "Regime is bull", "source": "prism.regimes.current", "url": None},
    ]

    annotated = annotate_citations(citations, jev_client=stub)

    assert len(stub.calls) == 1
    assert annotated[0] == {
        **citations[0],
        CITATION_TYPE_FIELD: {"type": "sec_filing", "source": "jev", "confidence": 0.78},
    }
    assert annotated[1] == citations[1]
    assert CITATION_TYPE_FIELD not in citations[0], "the stored rows are never mutated"


def test_annotate_packet_is_additive_and_fails_open() -> None:
    packet = {
        "ticker": "NVDA",
        "memo": {
            "text": "memo",
            "citations": [
                {"id": "C1", "claim": "10-K filed 2026-02-25", "source": "SEC EDGAR", "url": None}
            ],
        },
    }

    ok = annotate_packet_citations(packet, jev_client=_StubJevAskClient(choice="sec_filing"))
    assert ok["memo"]["text"] == "memo"
    assert ok["memo"]["citations"][0][CITATION_TYPE_FIELD]["type"] == "sec_filing"
    assert packet["memo"]["citations"][0] == {
        "id": "C1",
        "claim": "10-K filed 2026-02-25",
        "source": "SEC EDGAR",
        "url": None,
    }

    citation_module.clear_classify_cache()
    down = annotate_packet_citations(packet, jev_client=_StubJevAskClient(error=JevError("x")))
    assert down == packet

    assert annotate_packet_citations({"memo": None}) == {"memo": None}
    assert annotate_packet_citations("not a packet") == "not a packet"
