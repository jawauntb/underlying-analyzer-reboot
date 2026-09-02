"""HTTP contract tests for the Prism blueprint.

The routes are what the Mapvest API proxies and what the agent tools bind to, so
these tests pin the exact paths, request bodies, status codes and response
shapes - including the ``/api/ubermemo`` alias and the busy-signal headers.

Nothing here touches the network: the store is a temp directory, the engine is
stubbed where a real build would only be re-testing ``test_prism_narrative``, and
the text generator is a fake.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

from app.anthropic import GeneratedText
from app.main import create_app
from app.prism import engine as engine_module
from app.prism import routes as routes_module
from app.prism import store as store_module
from app.prism.contract import empty_packet
from app.tool_registry import build_request, get_tool

PRISM_PREFIXES = ("/api/prism", "/api/ubermemo")


class FakeTextGenerator:
    def __init__(self, text: str = "Prism answer [C1].") -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    def generate_text(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 700,  # noqa: ARG002
        temperature: float = 0.2,  # noqa: ARG002
    ) -> GeneratedText:
        self.calls.append({"system": system, "prompt": prompt})
        return GeneratedText(text=self.text, model="claude-test")


def _packet(ticker: str = "NVDA", as_of: str = "2026-09-01") -> dict[str, Any]:
    packet = empty_packet(ticker, as_of=as_of)
    packet["profile"] = {
        "name": f"{ticker} Corp",
        "sector": "Technology",
        "industry": "Semiconductors",
        "market_cap": 1_500_000_000_000.0,
        "related_etfs": ["SOXX", "XLK"],
    }
    packet["profile_error"] = None
    packet["scenarios"] = {
        "probability_horizon": "3m",
        "weights": {"seasonality": 0.5, "regime": 0.5},
        "components": {},
        "cases": {
            "bull": {
                "probability": 0.5,
                "narrative": "bull",
                "horizons": {"3m": {"probability": 0.5, "p50": 0.08, "price_p50": 240.0}},
            },
            "bear": {
                "probability": 0.2,
                "narrative": "bear",
                "horizons": {"3m": {"probability": 0.2, "p50": -0.07, "price_p50": 205.0}},
            },
            "neutral": {"probability": 0.3, "narrative": "neutral", "horizons": {}},
        },
        "entry": {
            "bargain_below": 205.0,
            "fair_value": 230.0,
            "expensive_above": 255.0,
            "current_price": 220.0,
            "current_vs_fair": -0.043,
        },
        "timing": {"this_month": "good", "reason": "September has paid historically"},
        "watch_signals": [],
    }
    packet["scenarios_error"] = None
    packet["regimes"] = {"current": {"label": "bull", "days_in_regime": 12,
                                     "switch_confidence": 0.6}}
    packet["regimes_error"] = None
    packet["memo"] = {
        "recommendation": {
            "action": "buy",
            "strength": "normal",
            "conviction": 0.55,
            "one_line": f"{ticker}: buy (normal).",
        },
        "entry_price": 205.0,
        "fair_value": 230.0,
        "stop_or_reassess": 205.0,
        "exit_targets": [{"horizon": "3m", "price": 240.0, "probability": 0.5}],
        "text": f"# {ticker} - Prism memo\n\nConstructive.",
        "key_determinants": [],
        "priced_in": [],
        "citations": [
            {"id": "C1", "claim": "Regime is bull", "source": "prism.regimes.current", "url": None}
        ],
        "model": None,
        "method": "deterministic",
    }
    packet["memo_error"] = None
    return packet


@pytest.fixture()
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Flask:
    monkeypatch.setenv("PRISM_CACHE_DIR", str(tmp_path))
    store_module.reset_default_store()
    application = create_app()
    application.config["PRISM_STORE"] = store_module.PrismStore(
        base_dir=tmp_path, supabase=None
    )
    application.config["PRISM_TEXT_GENERATOR"] = FakeTextGenerator()
    application.config["ANTHROPIC_API_KEY"] = None
    yield application
    store_module.reset_default_store()


@pytest.fixture()
def stored(app: Flask) -> dict[str, Any]:
    packet = _packet()
    app.config["PRISM_STORE"].save_packet(packet)
    return packet


# ---------------------------------------------------------------------------
# Route surface
# ---------------------------------------------------------------------------


def test_blueprint_is_mounted_under_both_prefixes(app: Flask) -> None:
    rules = {
        (rule.rule, method)
        for rule in app.url_map.iter_rules()
        for method in (rule.methods or set())
    }

    for prefix in PRISM_PREFIXES:
        assert (prefix, "POST") in rules
        assert (f"{prefix}/<ticker>", "GET") in rules
        assert (f"{prefix}/<ticker>/summary", "GET") in rules
        assert (f"{prefix}/<ticker>/export", "GET") in rules
        assert (f"{prefix}/chat", "POST") in rules


@pytest.mark.parametrize("prefix", PRISM_PREFIXES)
def test_info_route_describes_the_engine(app: Flask, prefix: str) -> None:
    response = app.test_client().get(f"{prefix}/")

    assert response.status_code == 200
    body = response.get_json()
    assert body["name"] == "Prism"
    assert body["alias"] == "ubermemo"
    assert body["routes"]["build"] == "POST /api/prism"


# ---------------------------------------------------------------------------
# POST /api/prism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", PRISM_PREFIXES)
def test_build_returns_the_packet_and_passes_through_the_flags(
    app: Flask, prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_build(client: Any, ticker: str, **kwargs: Any) -> dict[str, Any]:  # noqa: ARG001
        seen.update({"ticker": ticker, **kwargs})
        return _packet(ticker)

    monkeypatch.setattr(engine_module, "build_prism_packet", fake_build)

    response = app.test_client().post(
        prefix, json={"ticker": "nvda", "force": True, "include_memo": False}
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["ticker"] == "NVDA"
    assert body["memo"]["recommendation"]["action"] == "buy"
    assert seen["ticker"] == "NVDA"
    assert seen["force"] is True
    assert seen["include_memo"] is False
    assert seen["text_generator"] is app.config["PRISM_TEXT_GENERATOR"]


def test_build_rejects_a_missing_or_malformed_ticker(app: Flask) -> None:
    client = app.test_client()

    assert client.post("/api/prism", json={}).status_code == 400
    assert client.post("/api/prism", json={"ticker": ""}).status_code == 400
    assert client.post("/api/prism", json={"ticker": "A" * 20}).status_code == 400
    bad = client.post("/api/prism", json={"ticker": "NV DA"})
    assert bad.status_code == 400
    assert "unsupported characters" in bad.get_json()["error"]


def test_build_rejects_a_non_object_body(app: Flask) -> None:
    response = app.test_client().post(
        "/api/prism", data=json.dumps(["NVDA"]), content_type="application/json"
    )

    assert response.status_code == 400
    assert "JSON object" in response.get_json()["error"]


def test_build_failure_becomes_a_500_with_the_reason(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_: Any, **__: Any) -> dict[str, Any]:
        raise RuntimeError("massive is down")

    monkeypatch.setattr(engine_module, "build_prism_packet", explode)

    response = app.test_client().post("/api/prism", json={"ticker": "NVDA"})

    assert response.status_code == 500
    assert "massive is down" in response.get_json()["error"]


def test_process_capacity_returns_503_with_retry_after(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "build_prism_packet", lambda *_a, **_k: _packet())
    acquired = [
        routes_module._build_slots.acquire(blocking=False)
        for _ in range(routes_module.BUILD_CONCURRENCY_PER_PROCESS)
    ]
    try:
        response = app.test_client().post("/api/prism", json={"ticker": "NVDA"})
    finally:
        for slot in acquired:
            if slot:
                routes_module._build_slots.release()

    assert all(acquired)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == routes_module.DEFAULT_RETRY_AFTER
    assert "capacity" in response.get_json()["error"]


def test_one_build_per_external_client_returns_429(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "build_prism_packet", lambda *_a, **_k: _packet())
    assert routes_module.try_acquire_client("203.0.113.5")
    try:
        response = app.test_client().post(
            "/api/prism",
            json={"ticker": "NVDA"},
            headers={"X-Forwarded-For": "203.0.113.5"},
        )
    finally:
        routes_module.release_client("203.0.113.5")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == routes_module.DEFAULT_RETRY_AFTER


def test_loopback_callers_share_the_process_gate_instead_of_a_client_slot(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "build_prism_packet", lambda *_a, **_k: _packet())
    client = app.test_client()

    first = client.post("/api/prism", json={"ticker": "NVDA"})
    second = client.post("/api/prism", json={"ticker": "NVDA"})

    assert first.status_code == 200
    assert second.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/prism/<ticker> and /summary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", PRISM_PREFIXES)
def test_get_returns_the_stored_packet(app: Flask, stored: dict[str, Any], prefix: str) -> None:
    response = app.test_client().get(f"{prefix}/NVDA")

    assert response.status_code == 200
    body = response.get_json()
    assert body["ticker"] == "NVDA"
    assert body["as_of"] == stored["as_of"]
    assert body["memo"]["text"].startswith("# NVDA")


def test_get_is_case_insensitive(app: Flask, stored: dict[str, Any]) -> None:  # noqa: ARG001
    assert app.test_client().get("/api/prism/nvda").status_code == 200


def test_get_returns_404_when_nothing_is_stored(app: Flask) -> None:
    response = app.test_client().get("/api/prism/ZZZZ")

    assert response.status_code == 404
    assert "POST /api/prism to build one" in response.get_json()["error"]


def test_summary_is_the_bounded_agent_projection(app: Flask, stored: dict[str, Any]) -> None:
    response = app.test_client().get("/api/prism/NVDA/summary")

    assert response.status_code == 200
    body = response.get_json()
    assert body["ticker"] == "NVDA"
    assert body["recommendation"]["action"] == "buy"
    assert body["one_line"] == stored["memo"]["recommendation"]["one_line"]
    assert "seasonality" in body["unavailable_sections"]
    assert body["disclaimer"].startswith("Research only")
    assert len(response.get_data()) < 40_000


def test_summary_returns_404_without_a_packet(app: Flask) -> None:
    assert app.test_client().get("/api/prism/ZZZZ/summary").status_code == 404


# ---------------------------------------------------------------------------
# GET /api/prism/<ticker>/export
# ---------------------------------------------------------------------------


def test_export_defaults_to_text(app: Flask, stored: dict[str, Any]) -> None:  # noqa: ARG001
    response = app.test_client().get("/api/prism/NVDA/export")

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "text/plain; charset=utf-8"
    assert 'filename="prism-NVDA-2026-09-01.txt"' in response.headers["Content-Disposition"]
    assert b"PRISM MEMO - NVDA" in response.get_data()


def test_export_json_and_pdf(app: Flask, stored: dict[str, Any]) -> None:  # noqa: ARG001
    client = app.test_client()

    as_json = client.get("/api/prism/NVDA/export?format=json")
    assert as_json.status_code == 200
    assert as_json.headers["Content-Type"] == "application/json"
    assert json.loads(as_json.get_data())["ticker"] == "NVDA"

    as_pdf = client.get("/api/prism/NVDA/export?format=pdf")
    assert as_pdf.status_code == 200
    assert as_pdf.headers["Content-Type"] == "application/pdf"
    assert as_pdf.get_data()[:5] == b"%PDF-"
    assert as_pdf.headers["Content-Length"] == str(len(as_pdf.get_data()))


def test_export_rejects_an_unknown_format(app: Flask, stored: dict[str, Any]) -> None:  # noqa: ARG001
    response = app.test_client().get("/api/prism/NVDA/export?format=docx")

    assert response.status_code == 400
    assert "format must be one of" in response.get_json()["error"]


def test_export_returns_404_without_a_packet(app: Flask) -> None:
    assert app.test_client().get("/api/prism/ZZZZ/export").status_code == 404


# ---------------------------------------------------------------------------
# POST /api/prism/chat
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", PRISM_PREFIXES)
def test_chat_answers_from_the_stored_packet(
    app: Flask, stored: dict[str, Any], prefix: str  # noqa: ARG001
) -> None:
    response = app.test_client().post(
        f"{prefix}/chat", json={"ticker": "NVDA", "message": "What regime are we in?"}
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["ticker"] == "NVDA"
    assert body["reply"] == "Prism answer [C1]."
    assert [row["id"] for row in body["citations"]] == ["C1"]
    assert body["conversation_id"]
    assert body["history"][-1] == {"role": "assistant", "content": "Prism answer [C1]."}


def test_chat_threads_are_persisted_and_resumable(
    app: Flask, stored: dict[str, Any]  # noqa: ARG001
) -> None:
    client = app.test_client()
    generator = app.config["PRISM_TEXT_GENERATOR"]

    first = client.post(
        "/api/prism/chat", json={"ticker": "NVDA", "message": "First question?"}
    ).get_json()
    client.post(
        "/api/prism/chat",
        json={
            "ticker": "NVDA",
            "message": "Follow up?",
            "conversation_id": first["conversation_id"],
        },
    )

    assert "First question?" in generator.calls[-1]["prompt"]
    history = app.config["PRISM_STORE"].chat_history(first["conversation_id"])
    assert [row["role"] for row in history] == ["user", "assistant", "user", "assistant"]


def test_chat_validates_its_body(app: Flask, stored: dict[str, Any]) -> None:  # noqa: ARG001
    client = app.test_client()

    assert client.post("/api/prism/chat", json={"message": "hi"}).status_code == 400
    empty = client.post("/api/prism/chat", json={"ticker": "NVDA", "message": "  "})
    assert empty.status_code == 400
    assert "message is required" in empty.get_json()["error"]
    long = client.post(
        "/api/prism/chat",
        json={"ticker": "NVDA", "message": "x" * (routes_module.MAX_MESSAGE_LENGTH + 1)},
    )
    assert long.status_code == 400
    bad_history = client.post(
        "/api/prism/chat", json={"ticker": "NVDA", "message": "hi", "history": "nope"}
    )
    assert bad_history.status_code == 400
    assert "history must be a list" in bad_history.get_json()["error"]


def test_chat_without_a_packet_returns_404(app: Flask) -> None:
    response = app.test_client().post(
        "/api/prism/chat", json={"ticker": "ZZZZ", "message": "hello"}
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Tool registry binding
# ---------------------------------------------------------------------------


def test_prism_tools_bind_to_the_registered_routes(app: Flask) -> None:
    registered = {
        (rule.rule, method)
        for rule in app.url_map.iter_rules()
        for method in (rule.methods or set())
    }

    for name in ("prism_memo", "prism_get", "prism_chat", "prism_export"):
        spec = get_tool(name)
        flask_path = spec.path
        for param in spec.path_params:
            flask_path = flask_path.replace("{" + param + "}", f"<{param}>")
        assert (flask_path, spec.method) in registered, name


def test_prism_memo_carries_the_ubermemo_alias_and_resolves_by_it() -> None:
    from app.tool_registry import TOOL_ALIASES

    assert get_tool("prism_memo").aliases == ("ubermemo",)
    assert TOOL_ALIASES["ubermemo"] == "prism_memo"
    assert get_tool("ubermemo") is get_tool("prism_memo")


def test_tool_requests_resolve_to_the_exact_http_calls() -> None:
    assert build_request(get_tool("prism_memo"), {"ticker": "nvda", "force": True}) == (
        "POST",
        "/api/prism",
        {"ticker": "nvda", "force": True},
        {},
    )
    assert build_request(get_tool("prism_get"), {"ticker": "NVDA"}) == (
        "GET",
        "/api/prism/NVDA",
        None,
        {},
    )
    assert build_request(
        get_tool("prism_export"), {"ticker": "NVDA", "format": "pdf"}
    ) == ("GET", "/api/prism/NVDA/export", None, {"format": "pdf"})
    assert build_request(
        get_tool("prism_chat"), {"ticker": "NVDA", "message": "why?"}
    ) == ("POST", "/api/prism/chat", {"ticker": "NVDA", "message": "why?"}, {})


def test_prism_tools_are_documented_in_openapi() -> None:
    from app.openapi import build_openapi_document

    paths = build_openapi_document()["paths"]

    assert paths["/api/prism"]["post"]["operationId"] == "prism_memo"
    assert paths["/api/prism/{ticker}"]["get"]["operationId"] == "prism_get"
    export_parameters = {
        parameter["name"] for parameter in paths["/api/prism/{ticker}/export"]["get"]["parameters"]
    }
    assert {"ticker", "format"} == export_parameters
