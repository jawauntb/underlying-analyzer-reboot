"""HTTP contract tests for the Situate blueprint.

The routes are what Mapvest proxies and what the agent tools bind to, so these
tests pin the exact paths, request bodies, status codes and response shapes —
including the ``/api/research`` alias. Nothing touches the network: the store is
a temp directory, the engine build is stubbed, and the text generator is a fake.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from flask import Flask

from app.anthropic import GeneratedText
from app.main import create_app
from app.prism import store as store_module
from app.situate import engine as engine_module
from app.situate.contract import HORIZONS, empty_packet
from app.tool_registry import build_request, get_tool

SITUATE_PREFIXES = ("/api/situate", "/api/research")


class FakeTextGenerator:
    def __init__(self, text: str = "Situate answer [C1].") -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    def generate_text(
        self, *, system: str, prompt: str, max_tokens: int = 700, temperature: float = 0.2
    ) -> GeneratedText:
        del max_tokens, temperature
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
    packet["exposure"] = {
        "betas": {"SPY": 1.4, "SOXX": 0.9}, "r2": 0.7, "idiosyncratic_share": 0.3,
        "se": {"SPY": 0.1, "SOXX": 0.1}, "change_12m": {"SPY": 0.1, "SOXX": 0.0},
        "factor": {"loadings": {"MKT": 1.5}}, "version": "1.0.0",
    }
    packet["exposure_error"] = None
    packet["base_rates"] = {
        "by_horizon": {
            str(h): {
                "uncond": {"q50": 0.02},
                "shrunk": {"q05": -0.15, "q25": -0.03, "q50": 0.04, "q75": 0.11, "q95": 0.25,
                           "n_eff": 30, "w": 0.5},
                "industry": {"shrunk": {"q50": 0.01}},
            }
            for h in HORIZONS
        },
        "version": "1.0.0",
    }
    packet["base_rates_error"] = None
    from app.situate.odds import build_odds, build_scenarios

    packet["odds"] = build_odds(packet)
    packet["odds_error"] = None
    packet["scenarios"] = build_scenarios(packet, packet["odds"])
    packet["scenarios_error"] = None
    from app.situate.memo import fallback_memo

    packet["memo"] = fallback_memo(packet, reason="test fixture")
    packet["memo_error"] = None
    return packet


@pytest.fixture()
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Flask:
    monkeypatch.setenv("PRISM_CACHE_DIR", str(tmp_path))
    store_module.reset_default_store()
    application = create_app()
    application.config["SITUATE_STORE"] = store_module.PrismStore(
        base_dir=tmp_path / "situate", supabase=None
    )
    application.config["SITUATE_TEXT_GENERATOR"] = FakeTextGenerator()
    application.config["ANTHROPIC_API_KEY"] = None
    yield application
    store_module.reset_default_store()


@pytest.fixture()
def stored(app: Flask) -> dict[str, Any]:
    packet = _packet()
    app.config["SITUATE_STORE"].save_packet(packet)
    return packet


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------


def test_blueprint_is_mounted_under_both_prefixes(app: Flask) -> None:
    rules = {
        (rule.rule, method)
        for rule in app.url_map.iter_rules()
        for method in (rule.methods or set())
    }
    for prefix in SITUATE_PREFIXES:
        assert (prefix, "POST") in rules
        assert (f"{prefix}/<ticker>", "GET") in rules
        assert (f"{prefix}/<ticker>/summary", "GET") in rules
        assert (f"{prefix}/<ticker>/export", "GET") in rules
        assert (f"{prefix}/<ticker>/chat", "POST") in rules


@pytest.mark.parametrize("prefix", SITUATE_PREFIXES)
def test_info_route_describes_the_engine(app: Flask, prefix: str) -> None:
    response = app.test_client().get(f"{prefix}/")
    assert response.status_code == 200
    body = response.get_json()
    assert body["name"] == "Situate"
    assert body["routes"]["build"] == "POST /api/situate"
    assert body["routes"]["chat"] == "POST /api/situate/{ticker}/chat"


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", SITUATE_PREFIXES)
def test_build_returns_packet_and_passes_flags(
    app: Flask, prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def _stub(client: Any, ticker: str, **kwargs: Any) -> dict[str, Any]:
        del client
        seen.update({"ticker": ticker, **kwargs})
        return _packet(ticker)

    monkeypatch.setattr(engine_module, "build_situate_packet", _stub)
    response = app.test_client().post(
        prefix, json={"ticker": "nvda", "force": True, "include_memo": False}
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["ticker"] == "NVDA"
    assert seen["ticker"] == "NVDA"
    assert seen["force"] is True
    assert seen["include_memo"] is False


def test_build_rejects_missing_ticker(app: Flask) -> None:
    response = app.test_client().post("/api/situate", json={})
    assert response.status_code == 400
    assert "ticker is required" in response.get_json()["error"]


def test_build_rejects_bad_as_of(app: Flask) -> None:
    response = app.test_client().post(
        "/api/situate", json={"ticker": "NVDA", "as_of": "9999-99-99"}
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Read / summary
# ---------------------------------------------------------------------------


def test_get_returns_stored_packet(app: Flask, stored: dict[str, Any]) -> None:  # noqa: ARG001
    response = app.test_client().get("/api/situate/NVDA")
    assert response.status_code == 200
    assert response.get_json()["ticker"] == "NVDA"


def test_get_missing_is_404(app: Flask) -> None:
    response = app.test_client().get("/api/situate/ZZZZ")
    assert response.status_code == 404


def test_summary_projection(app: Flask, stored: dict[str, Any]) -> None:  # noqa: ARG001
    response = app.test_client().get("/api/situate/NVDA/summary")
    assert response.status_code == 200
    body = response.get_json()
    assert body["ticker"] == "NVDA"
    assert body["posture"]["stance"] in {"odds_favorable", "balanced", "odds_unfavorable"}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fmt,marker,ctype",
    [
        ("md", b"Situate", "text/markdown"),
        ("json", b'"engine": "Situate"', "application/json"),
        ("pdf", b"%PDF", "application/pdf"),
    ],
)
def test_export_formats(
    app: Flask, stored: dict[str, Any], fmt: str, marker: bytes, ctype: str  # noqa: ARG001
) -> None:
    response = app.test_client().get(f"/api/situate/NVDA/export?format={fmt}")
    assert response.status_code == 200
    assert ctype in response.headers["Content-Type"]
    assert marker in response.data
    assert "situate-NVDA" in response.headers["Content-Disposition"]


def test_export_rejects_unknown_format(app: Flask, stored: dict[str, Any]) -> None:  # noqa: ARG001
    response = app.test_client().get("/api/situate/NVDA/export?format=csv")
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


def test_chat_answers_from_the_packet(app: Flask, stored: dict[str, Any]) -> None:  # noqa: ARG001
    response = app.test_client().post(
        "/api/situate/NVDA/chat", json={"message": "what is the posture?"}
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["ticker"] == "NVDA"
    assert body["reply"]
    assert body["method"] == "model"  # the fake generator is configured
    assert "conversation_id" in body


def test_chat_requires_a_message(app: Flask, stored: dict[str, Any]) -> None:  # noqa: ARG001
    response = app.test_client().post("/api/situate/NVDA/chat", json={})
    assert response.status_code == 400


def test_chat_missing_packet_is_404(app: Flask) -> None:
    response = app.test_client().post("/api/situate/ZZZZ/chat", json={"message": "hi"})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Tool registry binding
# ---------------------------------------------------------------------------


def test_situate_tools_bind_to_the_routes() -> None:
    method, path, body, query = build_request(get_tool("situate"), {"ticker": "NVDA"})
    assert (method, path, body, query) == ("POST", "/api/situate", {"ticker": "NVDA"}, {})

    method, path, body, query = build_request(get_tool("situate_get"), {"ticker": "NVDA"})
    assert (method, path) == ("GET", "/api/situate/NVDA")

    method, path, body, query = build_request(
        get_tool("situate_chat"), {"ticker": "NVDA", "message": "why?"}
    )
    assert (method, path, body) == ("POST", "/api/situate/NVDA/chat", {"message": "why?"})

    method, path, _body, query = build_request(
        get_tool("situate_export"), {"ticker": "NVDA", "format": "pdf"}
    )
    assert (method, path, query) == ("GET", "/api/situate/NVDA/export", {"format": "pdf"})


def test_ubermemo_alias_still_resolves_to_prism(app: Flask) -> None:  # noqa: ARG001
    # Situate carries the ubermemo alias for migration, but the locked Prism
    # contract keeps ubermemo bound to prism_memo — that must not regress.
    from app.tool_registry import TOOL_ALIASES

    assert TOOL_ALIASES["ubermemo"] == "prism_memo"
