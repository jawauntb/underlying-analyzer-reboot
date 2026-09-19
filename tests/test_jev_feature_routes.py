"""HTTP contracts for the Jev-backed alert materiality and memo citation annotations.

``JevClient.ask`` is patched at the class so the routes exercise their real
default client wiring without a key or the network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from test_api import FakeMarketDataClient, FakeWatchlistClient

from app import alert_materiality as materiality_module
from app import citation_verify as citation_module
from app.jev import JevClient, JevError
from app.main import create_app
from app.prism import store as store_module
from app.prism.contract import empty_packet as prism_empty_packet
from app.situate.contract import empty_packet as situate_empty_packet

LEVELS = ("noise", "minor", "material", "urgent")


def _score_answer(level: str, confidence: float) -> dict[str, Any]:
    probabilities = dict.fromkeys(LEVELS, 0.02)
    probabilities[level] = 0.94
    return {
        "type": "score",
        "score": float(LEVELS.index(level)),
        "legend": {str(i): name for i, name in enumerate(LEVELS)},
        "probabilities": probabilities,
        "confidence": confidence,
    }


@pytest.fixture(autouse=True)
def _fresh_caches() -> Any:
    materiality_module.clear_materiality_cache()
    citation_module.clear_classify_cache()
    yield
    materiality_module.clear_materiality_cache()
    citation_module.clear_classify_cache()


@pytest.fixture()
def alerts_app(monkeypatch: pytest.MonkeyPatch) -> Flask:
    monkeypatch.setenv("UNDERLYING_SKIP_DOTENV", "1")
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    app.config["WATCHLIST_CLIENT"] = FakeWatchlistClient()
    return app


# ---------------------------------------------------------------------------
# POST /api/watchlists/alerts + jev_materiality
# ---------------------------------------------------------------------------


def test_alerts_carry_jev_materiality_scored_in_one_batch(
    alerts_app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_ask(_self: JevClient, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        calls.append({"state": state, "questions": questions})
        answers = {}
        for qid, item in zip(questions, state["alerts"], strict=True):
            level = "urgent" if item["severity"] == "High" else "minor"
            answers[qid] = _score_answer(level, 0.82)
        return answers

    monkeypatch.setattr(JevClient, "ask", fake_ask)
    client = alerts_app.test_client()

    response = client.post("/api/watchlists/alerts", json={"tickers": ["AAPL", "MSFT"]})

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["alerts"], "fixture rows fire at least one alert"
    assert len(calls) == 1
    assert len(calls[0]["questions"]) == len(payload["alerts"])
    for alert in payload["alerts"]:
        materiality = alert["jev_materiality"]
        assert materiality["level"] in LEVELS
        assert 0.0 <= materiality["score"] <= 1.0
        assert materiality["confidence"] == 0.82
        # Existing fields are untouched.
        assert {"id", "ticker", "severity", "category", "title", "message", "action"} <= set(
            alert
        )
    assert payload["export"]["alerts"] == payload["alerts"]
    assert payload["meta"]["min_materiality"] is None


def test_alerts_polling_does_not_re_score_identical_alerts(
    alerts_app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def fake_ask(_self: JevClient, _state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        calls.append(len(questions))
        return {qid: _score_answer("material", 0.9) for qid in questions}

    monkeypatch.setattr(JevClient, "ask", fake_ask)
    client = alerts_app.test_client()

    first = client.post("/api/watchlists/alerts", json={"tickers": ["AAPL", "MSFT"]}).get_json()
    second = client.post("/api/watchlists/alerts", json={"tickers": ["AAPL", "MSFT"]}).get_json()

    assert len(calls) == 1
    assert [a["jev_materiality"] for a in second["alerts"]] == [
        a["jev_materiality"] for a in first["alerts"]
    ]


def test_alerts_fail_open_when_jev_errors(
    alerts_app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_ask(_self: JevClient, _state: Any, _questions: dict[str, Any]) -> dict[str, Any]:
        raise JevError("timeout")

    monkeypatch.setattr(JevClient, "ask", broken_ask)
    client = alerts_app.test_client()

    response = client.post("/api/watchlists/alerts", json={"tickers": ["AAPL", "MSFT"]})

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["alerts"]
    assert all("jev_materiality" not in alert for alert in payload["alerts"])
    assert payload["meta"]["alert_count"] == len(payload["alerts"])


def test_alerts_fail_open_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNDERLYING_SKIP_DOTENV", "1")
    monkeypatch.delenv("JEV_API_KEY", raising=False)

    def never(*_: Any, **__: Any) -> Any:
        raise AssertionError("Jev must not be called without a key")

    monkeypatch.setattr(JevClient, "ask", never)
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    app.config["WATCHLIST_CLIENT"] = FakeWatchlistClient()

    response = app.test_client().post("/api/watchlists/alerts", json={"tickers": ["AAPL"]})

    assert response.status_code == 200
    assert all("jev_materiality" not in alert for alert in response.get_json()["alerts"])


def test_alerts_low_confidence_scores_are_omitted(
    alerts_app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_ask(_self: JevClient, _state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        return {qid: _score_answer("urgent", 0.4) for qid in questions}

    monkeypatch.setattr(JevClient, "ask", fake_ask)
    client = alerts_app.test_client()
    payload = client.post("/api/watchlists/alerts", json={"tickers": ["AAPL"]}).get_json()
    assert payload["alerts"]
    assert all("jev_materiality" not in alert for alert in payload["alerts"])


def test_min_materiality_filters_scored_alerts_and_keeps_unscored(
    alerts_app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Score by position: the first alert on the page is material, the second is
    # confidently noise. The fixture rows fire (at least) two alerts.
    def fake_ask(_self: JevClient, _state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        levels = ["material", "noise"]
        return {
            qid: _score_answer(levels[min(index, 1)], 0.9)
            for index, qid in enumerate(questions)
        }

    monkeypatch.setattr(JevClient, "ask", fake_ask)
    client = alerts_app.test_client()

    unfiltered = client.post(
        "/api/watchlists/alerts", json={"tickers": ["AAPL", "MSFT"]}
    ).get_json()
    filtered = client.post(
        "/api/watchlists/alerts?min_materiality=material", json={"tickers": ["AAPL", "MSFT"]}
    ).get_json()

    assert len(unfiltered["alerts"]) >= 2
    assert unfiltered["alerts"][0]["jev_materiality"]["level"] == "material"
    assert unfiltered["alerts"][1]["jev_materiality"]["level"] == "noise"
    assert [a["id"] for a in filtered["alerts"]] == [unfiltered["alerts"][0]["id"]]
    assert filtered["meta"]["min_materiality"] == "material"
    assert filtered["meta"]["alert_count"] == 1
    assert filtered["digest"]["severity_counts"] == {"High": 1}
    assert filtered["export"]["alerts"] == filtered["alerts"]

    # The body form works too, and an unknown floor is ignored rather than rejected.
    body = client.post(
        "/api/watchlists/alerts",
        json={"tickers": ["AAPL", "MSFT"], "min_materiality": "material"},
    ).get_json()
    assert [a["id"] for a in body["alerts"]] == [a["id"] for a in filtered["alerts"]]
    bogus = client.post(
        "/api/watchlists/alerts?min_materiality=whatever", json={"tickers": ["AAPL", "MSFT"]}
    ).get_json()
    assert len(bogus["alerts"]) == len(unfiltered["alerts"])
    assert bogus["meta"]["min_materiality"] is None


def test_min_materiality_keeps_alerts_jev_could_not_score(
    alerts_app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    # First alert: confidently noise (dropped). Second: unsure (kept, unscored).
    def fake_ask(_self: JevClient, _state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        return {
            qid: _score_answer("noise", 0.9 if index == 0 else 0.2)
            for index, qid in enumerate(questions)
        }

    monkeypatch.setattr(JevClient, "ask", fake_ask)
    client = alerts_app.test_client()

    unfiltered = client.post(
        "/api/watchlists/alerts", json={"tickers": ["AAPL", "MSFT"]}
    ).get_json()
    filtered = client.post(
        "/api/watchlists/alerts?min_materiality=material", json={"tickers": ["AAPL", "MSFT"]}
    ).get_json()

    assert len(unfiltered["alerts"]) >= 2
    assert "jev_materiality" not in unfiltered["alerts"][1]
    kept_ids = [a["id"] for a in filtered["alerts"]]
    assert unfiltered["alerts"][0]["id"] not in kept_ids
    assert unfiltered["alerts"][1]["id"] in kept_ids
    assert all("jev_materiality" not in a for a in filtered["alerts"])


# ---------------------------------------------------------------------------
# Prism / Situate memo citation annotations
# ---------------------------------------------------------------------------


def _prism_packet() -> dict[str, Any]:
    packet = prism_empty_packet("NVDA", as_of="2026-09-01")
    packet["memo"] = {
        "recommendation": {"action": "buy", "strength": "normal", "conviction": 0.5,
                           "one_line": "NVDA: buy."},
        "text": "# NVDA - Prism memo",
        "citations": [
            {"id": "C1", "claim": "Regime is bull", "source": "prism.regimes.current",
             "url": None},
            {"id": "C2", "claim": "10-K filed 2026-02-25 for the period ending 2025-12-31",
             "source": "SEC EDGAR", "url": "https://www.sec.gov/x"},
        ],
        "model": None,
        "method": "deterministic",
    }
    packet["memo_error"] = None
    return packet


def _situate_packet() -> dict[str, Any]:
    packet = situate_empty_packet("NVDA", as_of="2026-09-01")
    packet["memo"] = {
        "posture": {"stance": "constructive", "horizon": "3m", "conviction": 0.5,
                    "one_line": "NVDA: constructive."},
        "text": "# NVDA - Situate memo",
        "citations": [
            {"id": "C1", "claim": "SPY beta 1.4", "module": "exposure", "version": "1.0.0",
             "source": "exposure v1.0.0", "url": None},
            {"id": "C2", "claim": "10-Q filed 2026-05-20", "module": "text",
             "version": "1.0.0", "source": "SEC EDGAR", "url": "https://www.sec.gov/y"},
        ],
        "model": None,
        "method": "deterministic",
    }
    packet["memo_error"] = None
    return packet


@pytest.fixture()
def memo_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("UNDERLYING_SKIP_DOTENV", "1")
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    monkeypatch.setenv("PRISM_CACHE_DIR", str(tmp_path))
    store_module.reset_default_store()
    app = create_app()
    app.config["PRISM_STORE"] = store_module.PrismStore(base_dir=tmp_path, supabase=None)
    app.config["SITUATE_STORE"] = store_module.PrismStore(
        base_dir=tmp_path / "situate", supabase=None
    )
    app.config["ANTHROPIC_API_KEY"] = None
    app.config["PRISM_STORE"].save_packet(_prism_packet())
    app.config["SITUATE_STORE"].save_packet(_situate_packet())
    yield app
    store_module.reset_default_store()


def _sec_filing_ask(calls: list[dict[str, Any]]) -> Any:
    def fake_ask(_self: JevClient, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        calls.append({"state": state, "questions": questions})
        answers = {}
        for qid, item in zip(questions, state["citations"], strict=True):
            if "SEC EDGAR" in item["citation"]:
                answers[qid] = {"type": "choice", "choice": "sec_filing", "confidence": 0.78}
            else:
                answers[qid] = {"type": "choice", "choice": "unknown", "confidence": 0.9}
        return answers

    return fake_ask


@pytest.mark.parametrize(
    ("path", "expected_first"),
    [
        ("/api/prism/NVDA", {"id": "C1", "claim": "Regime is bull",
                             "source": "prism.regimes.current", "url": None}),
        ("/api/ubermemo/NVDA", {"id": "C1", "claim": "Regime is bull",
                                "source": "prism.regimes.current", "url": None}),
        ("/api/situate/NVDA", {"id": "C1", "claim": "SPY beta 1.4", "module": "exposure",
                               "version": "1.0.0", "source": "exposure v1.0.0", "url": None}),
        ("/api/research/NVDA", {"id": "C1", "claim": "SPY beta 1.4", "module": "exposure",
                                "version": "1.0.0", "source": "exposure v1.0.0", "url": None}),
    ],
)
def test_memo_routes_attach_citation_type_additively(
    memo_app: Flask, monkeypatch: pytest.MonkeyPatch, path: str, expected_first: dict[str, Any]
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(JevClient, "ask", _sec_filing_ask(calls))

    response = memo_app.test_client().get(path)

    assert response.status_code == 200
    citations = response.get_json()["memo"]["citations"]
    assert citations[0] == expected_first, "unknown types get no field at all"
    assert citations[1]["citation_type"] == {
        "type": "sec_filing",
        "source": "jev",
        "confidence": 0.78,
    }
    assert citations[1]["source"] == "SEC EDGAR", "the row's own source field is untouched"
    assert len(calls) == 1, "one batched Jev call per memo"


def test_memo_routes_are_byte_identical_when_jev_is_down(
    memo_app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_ask(_self: JevClient, _state: Any, _questions: dict[str, Any]) -> dict[str, Any]:
        raise JevError("529")

    monkeypatch.setattr(JevClient, "ask", broken_ask)
    client = memo_app.test_client()

    prism = client.get("/api/prism/NVDA").get_json()
    situate = client.get("/api/situate/NVDA").get_json()

    assert prism["memo"]["citations"] == _prism_packet()["memo"]["citations"]
    assert situate["memo"]["citations"] == _situate_packet()["memo"]["citations"]


def test_memo_annotation_does_not_mutate_the_stored_packet(
    memo_app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(JevClient, "ask", _sec_filing_ask(calls))
    client = memo_app.test_client()

    annotated = client.get("/api/prism/NVDA").get_json()
    assert "citation_type" in annotated["memo"]["citations"][1]

    stored = memo_app.config["PRISM_STORE"].load_packet("NVDA", "2026-09-01")
    assert stored is not None
    assert "citation_type" not in stored["memo"]["citations"][1]
