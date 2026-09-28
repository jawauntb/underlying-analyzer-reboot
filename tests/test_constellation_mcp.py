"""The constellation MCP (``POST /mcp``): what it offers, what it refuses, and what it reaches.

Every test goes through the real app (``create_app()`` and Flask's test client) with fakes in
``app.config``. The ``_offline`` fixture fails a test that tries to connect to anything but this
machine, so none reaches Anthropic, Exa, Massive, Modal, OpenAI or the network. The stub peer in
the hop tests is an HTTP server on 127.0.0.1.

What the tests show, in the order of the file:

* the protocol surface is the library's (initialize, tools/list, the 405, the manifest, the
  relay) and ``/api/mcp`` still answers as before;
* ``tools/list`` is the allowlist plus ``ask_lattice_animals``, and a registry tool that is not
  on the list is an unknown tool whatever its ``mcp`` flag says;
* each allowlisted tool answers on the happy path, with a result under the library's clip;
* each one refuses a bad ticker, an unknown argument and a wrong type, and a refused call
  reaches no route at all;
* the routes the tools do reach are recorded, and none is a build, a chat, an export or the
  ticker-research route with its admission limit;
* text that leaves the server is scrubbed, and the hop rule holds against a stub peer.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from flask import Flask, request
from flask.testing import FlaskClient

from app import constellation_mcp as cm
from app import peer_forecast as pf
from app import tabular_routes, tool_registry
from app.main import create_app
from app.market_data import HistoryResult, MarketDataError, OptionChainResult
from app.mcp_lite import MAX_HOP
from app.prism import engine as prism_engine
from app.prism import store as store_module
from app.prism.contract import empty_packet as prism_empty_packet
from app.situate import engine as situate_engine
from app.situate import peers
from app.situate.contract import empty_packet as situate_empty_packet
from app.tabular import TabularPrediction, TabularUnavailable
from app.tool_registry import TOOLS, TOOLS_BY_NAME

LIBRARY_CLIP = 8000  # what app.mcp_lite keeps of a tool's text
SECTOR = "technology"
UNIVERSE = list(peers.PEERS_BY_SECTOR[SECTOR])
PANEL_SYMBOLS = [*UNIVERSE, "XLK"]

# --------------------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------------------


class FakeMarket:
    """The market data client: 260 daily bars per ticker and a small option chain."""

    provider_label = "fake"
    provider_note = "fake test provider"
    fallback_enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def get_history(self, ticker: str, **kwargs: Any) -> HistoryResult:
        self.calls.append(("get_history", ticker, dict(kwargs)))
        dates = pd.date_range("2025-01-01", periods=260)
        close = [100.5 + index * 0.1 for index in range(260)]
        frame = pd.DataFrame(
            {
                "Open": [value - 0.5 for value in close],
                "High": [value + 1 for value in close],
                "Low": [value - 1 for value in close],
                "Close": close,
                "Adj Close": close,
                "Volume": [1_000_000 + index for index in range(260)],
            },
            index=dates,
        )
        return HistoryResult(
            ticker=ticker,
            data=frame,
            provider="fake",
            note="fake test provider",
            interval=str(kwargs.get("interval") or "1d"),
        )

    def get_profile(self, ticker: str) -> dict[str, Any]:
        self.calls.append(("get_profile", ticker, None))
        return {"longName": f"{ticker} Inc", "sector": "Testing", "marketCap": 123_000_000}

    def get_option_chain(self, ticker: str, expiry: str | None = None) -> OptionChainResult:
        self.calls.append(("get_option_chain", ticker, expiry))
        rows = []
        for strike in range(92, 110, 2):
            call_oi, put_oi = float(1000 - strike), float(strike * 3)
            rows.append(
                {
                    "strike": float(strike),
                    "call_open_interest": call_oi,
                    "put_open_interest": put_oi,
                    "call_last": 1.25,
                    "put_last": 0.75,
                    "net_open_interest": call_oi - put_oi,
                    "put_call_ratio": put_oi / call_oi,
                    "call_contract": f"O:{ticker}261016C{strike:08d}",
                    "call_implied_volatility": 0.31,
                    "call_delta": 0.5,
                    "call_bid": 1.2,
                    "call_ask": 1.3,
                    "put_bid": 0.7,
                    "put_ask": 0.8,
                }
            )
        return OptionChainResult(
            ticker=ticker,
            expiry=expiry or "2026-10-16",
            current_price=100.0,
            rows=rows,
            expirations=["2026-10-16", "2026-10-23"],
            provider="fake",
            note="fake test provider",
        )


class FakeSec:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_source_pack(self, ticker: str) -> dict[str, Any]:
        self.calls.append(ticker)
        section = {
            "Item": "Item 1",
            "Heading": "Business",
            "Snippet": "Apple sells products and services. " * 60,
            "Form": "10-K",
            "Filing Date": "2025-10-31",
            "Source URL": "https://www.sec.gov/Archives/example/aapl-10k.htm",
        }
        return {
            "Status": "available",
            "Provider": "SEC EDGAR",
            "Ticker": ticker,
            "CIK": "0000320193",
            "Company Name": "Apple Inc.",
            "SIC": "3571",
            "SIC Description": "Electronic Computers",
            "Exchanges": ["Nasdaq"],
            "Filings": {
                "10-K": {
                    "form": "10-K",
                    "filing_date": "2025-10-31",
                    "report_date": "2025-09-27",
                    "accession_number": "0000320193-25-000079",
                    "primary_document": "aapl-10k.htm",
                    "url": "https://www.sec.gov/Archives/example/aapl-10k.htm",
                }
            },
            "Filing Sections": {"Business": section, "Risk Factors": section, "MD&A": section},
            "Earnings Sections": {"Earnings Release": {**section, "Form": "8-K"}},
            "Company Facts": {
                "Revenue": {
                    "Value": 391_035_000_000,
                    "Unit": "USD",
                    "Form": "10-K",
                    "Filed": "2025-10-31",
                    "End Date": "2025-09-27",
                    "Fiscal Year": 2025,
                    "Fiscal Period": "FY",
                    "Concept": "RevenueFromContractWithCustomerExcludingAssessedTax",
                    "Description": "d" * 300,
                }
            },
            "Citations": [{"Label": "SEC 10-K Item 1 Business", "Type": "filing-section"}] * 4,
            "Errors": [],
        }


class RecordingPredictor:
    """A fake in-context classifier: leans 'over' when 12-1 momentum is positive."""

    name = "fake"

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def predict(
        self,
        task: str,
        x_context: pd.DataFrame,
        y_context: pd.Series,
        x_query: pd.DataFrame,
        *,
        categorical: Any = (),
        options: dict[str, Any] | None = None,
    ) -> TabularPrediction:
        del y_context, categorical, options
        if self.fail:
            raise TabularUnavailable("fake model is down")
        self.calls += 1
        classes = list(pf.BUCKETS)
        probabilities: list[list[float]] = []
        for value in x_query["mom_12_1"].fillna(0.0):
            base = (
                np.array([0.05, 0.10, 0.15, 0.25, 0.45])
                if value > 0
                else np.array([0.45, 0.25, 0.15, 0.10, 0.05])
            )
            probabilities.append((base / base.sum()).tolist())
        return TabularPrediction(
            task=task,
            predictions=[classes[int(np.argmax(row))] for row in probabilities],
            context_rows_used=int(x_context.shape[0]),
            classes=classes,
            probabilities=probabilities,
        )


class _History:
    def __init__(self, frame: pd.DataFrame) -> None:
        self.data = frame
        self.dataframe = frame


class FakePanelClient:
    """Daily closes for the technology universe and XLK, ending today, so the peer forecast
    works without an ``as_of`` (the tool passes none)."""

    def __init__(self, *, days: int = 2600, seed: int = 3) -> None:
        rng = np.random.default_rng(seed)
        index = pd.date_range(end=date.today().isoformat(), periods=days, freq="B")
        market = rng.normal(0.0004, 0.011, days)
        self.series: dict[str, pd.Series] = {}
        for offset, symbol in enumerate(PANEL_SYMBOLS):
            beta = 0.8 + 0.05 * (offset % 7)
            idio = rng.normal(0.0001 * (offset % 3), 0.009, days)
            prices = 100.0 * np.exp(np.cumsum(beta * market + idio))
            self.series[symbol] = pd.Series(prices, index=index, name=symbol)

    def get_history(self, ticker: str, *, start: Any, end: Any, interval: str = "1d") -> _History:
        del interval
        symbol = str(ticker).upper()
        if symbol not in self.series:
            raise ValueError(f"unknown symbol {symbol}")
        series = self.series[symbol]
        inside = (series.index >= pd.Timestamp(start)) & (series.index <= pd.Timestamp(end))
        windowed = series[inside]
        return _History(pd.DataFrame({"Close": windowed}))

    def get_profile(self, ticker: str) -> dict[str, Any]:
        return {"longName": f"{ticker} Corp", "sector": "Technology", "industry": "Software"}


def situate_packet(ticker: str = "NVDA", as_of: str = "2026-09-01") -> dict[str, Any]:
    packet = situate_empty_packet(ticker, as_of=as_of)
    packet["profile"] = {"name": f"{ticker} Corp", "sector": "Technology", "industry": "Chips"}
    packet["exposure"] = {"betas": {"SPY": 1.4, "SOXX": 0.9}, "r2": 0.7, "idiosyncratic_share": 0.3}
    packet["memo"] = {
        "posture": {"label": "balanced", "one_line": f"{ticker}: the odds are balanced."},
        "falsifiers": ["A close below the 200-day average."],
        "whats_priced_in": ["A recovery in margins."],
        "zones": {"cheap_below": 90.0, "rich_above": 130.0},
        "text": "The odds are balanced. " * 200,
    }
    return packet


def prism_packet(ticker: str = "NVDA", as_of: str = "2026-09-01") -> dict[str, Any]:
    packet = prism_empty_packet(ticker, as_of=as_of)
    packet["profile"] = {"name": f"{ticker} Corp", "sector": "Technology", "industry": "Chips"}
    packet["memo"] = {
        "recommendation": {"action": "buy", "one_line": f"{ticker}: buy (normal)."},
        "entry_price": 205.0,
        "fair_value": 230.0,
        "exit_targets": [{"horizon": "3m", "price": 240.0, "probability": 0.5}],
    }
    return packet


# --------------------------------------------------------------------------------------
# the world a test runs in
# --------------------------------------------------------------------------------------


@dataclass
class World:
    app: Flask
    client: FlaskClient
    market: FakeMarket
    sec: FakeSec
    predictor: RecordingPredictor
    routes: list[tuple[str, str]]  # every in-process request the app served, apart from /mcp


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test that tries to reach anything but this machine (the stub peer is local)."""
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

    def local_only(address: Any) -> None:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"a test tried to reach {host}")

    def guarded_connect(self: socket.socket, address: Any) -> None:
        local_only(address)
        connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> int:
        local_only(address)
        return connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    for name in (
        "MCP_PUBLIC_ORIGIN",
        "LATTICE_MCP_URL",
        "MCP_ALLOW_LOCAL",
        "MASSIVE_API_KEY",
        "TABULAR_INFERENCE_URL",
        "TABULAR_INFERENCE_TOKEN",
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "http_proxy",
        "HTTP_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UNDERLYING_SKIP_DOTENV", "1")
    monkeypatch.setenv("PRISM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("PRISM_CACHE_ENABLED", "0")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    pf.clear_peer_forecast_cache()
    store_module.reset_default_store()
    yield
    pf.clear_peer_forecast_cache()
    store_module.reset_default_store()


def make_world(tmp_path: Path) -> World:
    app = create_app()
    world = World(app, app.test_client(), FakeMarket(), FakeSec(), RecordingPredictor(), [])
    app.config["MARKET_DATA_CLIENT"] = world.market
    app.config["SEC_CLIENT"] = world.sec
    app.config["SITUATE_STORE"] = store_module.PrismStore(
        base_dir=tmp_path / "situate", supabase=None
    )
    app.config["PRISM_STORE"] = store_module.PrismStore(base_dir=tmp_path / "prism", supabase=None)
    app.config["TABULAR_PREDICTOR"] = world.predictor
    app.config["SITUATE_MARKET_CLIENT"] = FakePanelClient()

    def record() -> None:
        if not request.path.startswith(("/mcp", "/.well-known")):
            world.routes.append((request.method, request.path))

    app.before_request(record)
    return world


@pytest.fixture()
def world(tmp_path: Path) -> World:
    return make_world(tmp_path)


def rpc(
    client: FlaskClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    path: str = "/mcp",
    headers: dict[str, str] | None = None,
    request_id: int = 1,
) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    response = client.post(path, json=body, headers=headers)
    payload = response.get_json()
    assert isinstance(payload, dict)
    return payload


def call(
    world: World,
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A tools/call: the ``result`` object. It fails the test on a JSON-RPC error."""
    payload = rpc(
        world.client,
        "tools/call",
        {"name": name, "arguments": arguments or {}},
        headers=headers,
    )
    assert "error" not in payload, payload
    result = payload["result"]
    assert isinstance(result, dict)
    return result


def text_of(result: dict[str, Any]) -> str:
    text = result["content"][0]["text"]
    assert isinstance(text, str)
    assert len(text) <= LIBRARY_CLIP
    return text


def data_of(result: dict[str, Any]) -> Any:
    assert not result.get("isError"), text_of(result)
    return json.loads(text_of(result))


# --------------------------------------------------------------------------------------
# the protocol surface, through the real app
# --------------------------------------------------------------------------------------


def test_initialize_ping_and_notification(world: World) -> None:
    init = rpc(world.client, "initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert init["protocolVersion"] == "2025-06-18"
    assert init["serverInfo"]["name"] == "underlying-analyzer"
    assert init["capabilities"]["tools"] == {"listChanged": False}
    assert rpc(world.client, "ping")["result"] == {}
    note = world.client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status_code == 202 and not note.get_data()


def test_tools_list_is_exactly_the_allowlist_and_the_lattice_tool(world: World) -> None:
    tools = rpc(world.client, "tools/list")["result"]["tools"]
    assert [tool["name"] for tool in tools] == list(cm.CONSTELLATION_TOOLS)
    assert cm.CONSTELLATION_TOOLS[-1] == "ask_lattice_animals"
    assert len(cm.CONSTELLATION_TOOLS) == len(set(cm.CONSTELLATION_TOOLS)) == 11
    for tool in tools:
        assert tool["description"], tool["name"]
        assert len(tool["description"]) <= 300, f"{tool['name']}: the manifest keeps 300"
        schema = tool["inputSchema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema.get("required", [])) <= set(schema["properties"])
        # Read-only is what these tools are; the hint comes from the library. Only the tool that
        # calls another site is open-world.
        assert tool["annotations"]["readOnlyHint"] is True, tool["name"]
        assert tool["annotations"]["openWorldHint"] is (tool["name"] == "ask_lattice_animals")


@pytest.mark.parametrize(
    "body",
    ["{nope", "x" * 70_000, "[" * 20_000 + "]" * 20_000, '{"hello": 1}', '[{"jsonrpc": "2.0"}]'],
    ids=["not-json", "over-64k", "deeply-nested", "not-json-rpc", "batch"],
)
def test_a_malformed_body_is_a_json_rpc_error_and_not_a_500(world: World, body: str) -> None:
    response = world.client.post("/mcp", data=body, content_type="application/json")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == -32600
    assert world.routes == []


def test_get_mcp_is_405_with_allow_post(world: World) -> None:
    for method in ("get", "delete"):
        response = getattr(world.client, method)("/mcp")
        assert response.status_code == 405
        assert response.headers["Allow"] == "POST"
        assert response.get_json()["error"]["code"] == -32000


def test_post_mcp_is_not_redirected(world: World) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    for path in ("/mcp", "/mcp/"):
        response = world.client.post(path, json=body, follow_redirects=False)
        assert response.status_code == 200, path
        assert "Location" not in response.headers, path
    # the routes are registered exactly once each
    rules = {
        (rule.rule, method)
        for rule in world.app.url_map.iter_rules()
        for method in rule.methods or set()
    }
    assert ("/mcp", "POST") in rules and ("/mcp/<peer>", "POST") in rules
    assert ("/.well-known/mcp.json", "GET") in rules
    assert ("/.well-known/mcp/server-card.json", "GET") in rules


def test_manifest_and_server_card(world: World) -> None:
    for path in ("/.well-known/mcp.json", "/.well-known/mcp/server-card.json"):
        response = world.client.get(path)
        assert response.status_code == 200
        manifest = response.get_json()
        assert manifest["name"] == "underlying-analyzer"
        assert manifest["endpoint"] == "https://underlying-terminal-production.up.railway.app/mcp"
        assert manifest["transport"] == "streamable-http" and manifest["auth"] == "none"
        assert [tool["name"] for tool in manifest["tools"]] == list(cm.CONSTELLATION_TOOLS)
        assert all(tool["readOnly"] is True for tool in manifest["tools"])
        assert manifest["peers"] == [
            {
                "name": "lattice",
                "endpoint": "https://underlying-terminal-production.up.railway.app/mcp/lattice",
                "about": "",
            }
        ]
        assert manifest["hop"] == {"max": MAX_HOP, "headers": ["x-mcp-hop", "x-mcp-path"]}
        assert response.headers["Access-Control-Allow-Origin"] == "*"


def test_environment_sets_the_origin_and_the_hub(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    default = make_world(tmp_path)
    assert default.app.config["CONSTELLATION_MCP"].peers.get("lattice")["host"] == (
        "latticeanimal-production.up.railway.app"
    )
    monkeypatch.setenv("MCP_PUBLIC_ORIGIN", "https://mine.example/")
    monkeypatch.setenv("LATTICE_MCP_URL", "https://hub.example/mcp")
    custom = make_world(tmp_path)
    assert custom.client.get("/.well-known/mcp.json").get_json()["endpoint"] == (
        "https://mine.example/mcp"
    )
    assert custom.app.config["CONSTELLATION_MCP"].peers.get("lattice")["url"] == (
        "https://hub.example/mcp"
    )


def test_the_terminals_own_mcp_is_still_the_whole_registry(world: World) -> None:
    descriptor = world.client.get("/api/mcp").get_json()
    assert descriptor["endpoint"] == "/api/mcp" and descriptor["tool_count"] == len(TOOLS)
    listed = rpc(world.client, "tools/list", path="/api/mcp")["result"]["tools"]
    assert len(listed) == len(TOOLS) == 30
    assert {"analyze_ticker", "ticker_research_bundle", "situate"} <= {t["name"] for t in listed}
    health = rpc(
        world.client, "tools/call", {"name": "health_check", "arguments": {}}, path="/api/mcp"
    )
    assert health["result"]["isError"] is False
    assert json.loads(health["result"]["content"][0]["text"])["result"]["ok"] is True
    # and the two are separate: the allowlist is not the registry
    assert len(rpc(world.client, "tools/list")["result"]["tools"]) == 11


# --------------------------------------------------------------------------------------
# the allowlist decides, not the registry
# --------------------------------------------------------------------------------------

PAID_OR_GATED = (
    "analyze_ticker",  # calls a text model
    "analyze_batch",
    "stock_fax",
    "vision_memo",
    "ticker_research_bundle",  # the route with the loopback-exempt admission limit
    "situate",  # builds a packet: Massive, FRED, EDGAR, Exa and a model
    "prism_memo",
    "situate_chat",
    "prism_chat",
    "pixel_image",  # image generation
    "render_chart",
    "watchlist_cockpit",
    "resolve_watchlist",  # takes a URL
    "search_news",
    "compose_research_article",
)


def test_every_registry_tool_off_the_list_is_an_unknown_tool(world: World) -> None:
    off_the_list = sorted(set(TOOLS_BY_NAME) - set(cm.ALLOWED_REGISTRY_TOOLS))
    assert set(PAID_OR_GATED) <= set(off_the_list)
    assert len(off_the_list) == len(TOOLS) - len(cm.ALLOWED_REGISTRY_TOOLS) == 21
    for name in off_the_list:
        payload = rpc(world.client, "tools/call", {"name": name, "arguments": {"ticker": "AAPL"}})
        assert payload["error"]["code"] == -32602, name
        assert "unknown tool" in payload["error"]["message"], name
    # nothing ran: no route was reached, and no upstream client was asked
    assert world.routes == []
    assert world.market.calls == [] and world.sec.calls == []


def test_a_tool_name_that_is_not_a_tool_is_unknown(world: World) -> None:
    for name in ("rm_rf", "ubermemo", "list_capabilities ", "HEALTH_CHECK", "", None, 7):
        payload = rpc(world.client, "tools/call", {"name": name})
        assert payload["error"]["code"] == -32602, name
    assert world.routes == []


def test_the_registry_mcp_flag_does_not_decide(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flip every ToolSpec.mcp the other way: what /mcp offers does not move."""
    baseline = [t["name"] for t in rpc(world.client, "tools/list")["result"]["tools"]]
    for flag in (False, True):
        flipped = tuple(replace(spec, mcp=flag) for spec in TOOLS)
        monkeypatch.setattr(tool_registry, "TOOLS", flipped)
        monkeypatch.setattr(tool_registry, "TOOLS_BY_NAME", {spec.name: spec for spec in flipped})
        assert [t["name"] for t in rpc(world.client, "tools/list")["result"]["tools"]] == baseline
        assert data_of(call(world, "health_check")) == {
            "ok": True,
            "service": "underlying-analyzer-reboot",
        }
        hidden = rpc(world.client, "tools/call", {"name": "analyze_ticker", "arguments": {}})
        assert hidden["error"]["code"] == -32602, flag


def test_the_allowlist_names_cheap_registry_tools_and_no_gated_route() -> None:
    gated = {
        ("POST", "/api/data/ticker-research"),  # the 2 per process admission limit
        ("POST", "/api/situate"),
        ("POST", "/api/prism"),
        ("POST", "/api/situate/{ticker}/chat"),
        ("POST", "/api/prism/chat"),
    }
    for name in cm.ALLOWED_REGISTRY_TOOLS:
        spec = TOOLS_BY_NAME[name]
        assert spec.cost != tool_registry.COST_LLM, name
        assert not spec.produces_images, name
        assert (spec.method, spec.path) not in gated, name


# --------------------------------------------------------------------------------------
# each tool, on the happy path
# --------------------------------------------------------------------------------------


def test_health_check(world: World) -> None:
    result = call(world, "health_check")
    assert data_of(result) == {"ok": True, "service": "underlying-analyzer-reboot"}
    assert result["_meta"]["constellation"] == {
        "server": "underlying-analyzer",
        "hop": 0,
        "path": ["underlying-analyzer"],
    }
    assert world.routes == [("GET", "/api/health")]


def test_provider_status_says_whether_massive_is_configured_and_omits_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MASSIVE_API_KEY", "massive-key-9f8e7d6c5b4a")
    keyed = make_world(tmp_path)
    keyed.app.config["MARKET_DATA_CLIENT"] = create_app().config["MARKET_DATA_CLIENT"]
    text = text_of(call(keyed, "provider_status"))
    status = json.loads(text)
    assert status["primary"] == "massive" and status["massive_configured"] is True
    assert "massive-key-9f8e7d6c5b4a" not in text
    assert keyed.routes == [("GET", "/api/providers")]


def test_list_capabilities_says_which_tools_are_offered_here(world: World) -> None:
    result = call(world, "list_capabilities")
    catalog = data_of(result)
    offered = [row["name"] for row in catalog["callable_here"]]
    elsewhere = [row["name"] for row in catalog["not_offered_here"]]
    assert set(offered) == set(cm.CONSTELLATION_TOOLS) and len(offered) == len(set(offered))
    assert catalog["terminal_tools"] == len(TOOLS)
    assert set(elsewhere) == set(TOOLS_BY_NAME) - set(cm.ALLOWED_REGISTRY_TOOLS)
    assert not set(offered) & set(elsewhere)
    assert {"analyze_ticker", "ticker_research_bundle", "situate", "prism_memo"} <= set(elsewhere)
    assert all(row["cost"] in {"fast", "slow", "llm"} for row in catalog["callable_here"])
    assert len(text_of(result)) <= cm.RESULT_BUDGET
    assert world.routes == []  # the catalog is read from the registry, not a route


def test_sec_source_pack_is_a_summary(world: World) -> None:
    pack = data_of(call(world, "sec_source_pack", {"ticker": " aapl "}))
    assert world.sec.calls == ["AAPL"]
    assert pack["company"] == "Apple Inc." and pack["cik"] == "0000320193"
    assert pack["filings"]["10-K"]["accession_number"] == "0000320193-25-000079"
    assert pack["company_facts"]["Revenue"]["value"] == 391_035_000_000
    assert pack["citations"] == 4
    assert set(pack["filing_sections"]) == {"Business", "Risk Factors", "MD&A"}
    for section in (*pack["filing_sections"].values(), *pack["earnings_sections"].values()):
        assert 0 < len(section["excerpt"]) <= 300
    assert "Description" not in json.dumps(pack)
    assert world.routes == [("GET", "/api/sec/AAPL")]


@pytest.mark.parametrize("chart_type", cm.CHART_PACKS)
def test_chart_data_summarizes_each_pack(world: World, chart_type: str) -> None:
    result = call(world, "chart_data", {"chart_type": chart_type, "ticker": "aapl"})
    body = data_of(result)
    assert body["provider"] == "fake" and body["datasets"], chart_type
    assert len(text_of(result)) <= cm.RESULT_BUDGET
    assert world.routes == [("POST", f"/api/data/charts/{chart_type}")]
    assert "images" not in json.dumps(body) and "export" not in body


def test_ridge_growth_returns_every_window_and_the_series_of_the_one_year_window(
    world: World,
) -> None:
    result = call(world, "chart_data", {"chart_type": "ridge-growth", "ticker": "AAPL"})
    body = data_of(result)
    assert [window["period"] for window in body["windows"]] == ["6mo", "1y", "2y"]
    assert all(window["recommendation"] and window["state"] for window in body["windows"])
    (primary,) = body["datasets"]
    assert primary["period"] == "1y" and primary["series"]["close"]["points"] == 260
    assert "truncated" not in body and len(text_of(result)) <= cm.RESULT_BUDGET


def test_chart_data_keeps_the_last_points_of_a_series_not_the_whole_series(world: World) -> None:
    body = data_of(call(world, "chart_data", {"chart_type": "auction", "ticker": "AAPL"}))
    dataset = body["datasets"][0]
    assert dataset["ticker"] == "AAPL" and {"vah", "val", "poc"} <= set(dataset["levels"])
    close = dataset["series"]["close"]
    assert close["points"] == 260 and len(close["last"]) == cm.SERIES_TAIL
    assert close["from"] == "2025-01-01" and close["to"] == close["last"][-1]["date"]
    assert close["last"][-1]["value"] == pytest.approx(126.4)
    assert dataset["series"]["ohlcv"]["points"] == 260


def test_chart_data_passes_the_validated_options_to_the_route(world: World) -> None:
    call(
        world,
        "chart_data",
        {"chart_type": "regression", "ticker": "msft", "period": "6mo", "interval": "1w"},
    )
    (_, ticker, options), *_ = world.market.calls
    assert ticker == "MSFT" and options["period"] == "6mo" and options["interval"] == "1w"
    call(world, "chart_data", {"chart_type": "performance", "ticker": "AAPL", "month": 3})
    assert world.routes[-1] == ("POST", "/api/data/charts/performance")


def test_torque_data_is_a_summary_without_the_duplicates(world: World) -> None:
    result = call(world, "torque_data", {"ticker": "AAPL"})
    body = data_of(result)
    assert body["ticker"] == "AAPL" and body["meta"]["stage_label"]
    assert isinstance(body["meta"]["total_score"], int | float)
    assert "torque" not in body and "export" not in body
    assert body["series"]["price"]["close"]["last"]
    assert len(text_of(result)) <= cm.RESULT_BUDGET
    assert world.routes == [("POST", "/api/data/tools/torque")]


def test_moneyline_data_keeps_the_seven_ladder_fields(world: World) -> None:
    result = call(world, "moneyline_data", {"ticker": "aapl", "expiry": "2026-10-16"})
    body = data_of(result)
    assert world.market.calls == [("get_option_chain", "AAPL", "2026-10-16")]
    assert body["expiry"] == "2026-10-16" and body["current_price"] == 100.0
    assert body["strike_count"] == 9 == len(body["strikes"])
    assert set(body["strikes"][0]) == {
        "strike",
        "call_open_interest",
        "put_open_interest",
        "call_last",
        "put_last",
        "net_open_interest",
        "put_call_ratio",
    }
    assert world.routes == [("POST", "/api/data/tools/moneyline")]
    call(world, "moneyline_data", {"ticker": "AAPL"})  # no expiry: the nearest
    assert world.market.calls[-1] == ("get_option_chain", "AAPL", None)


def test_situate_get_reads_the_stored_summary(world: World) -> None:
    store = world.app.config["SITUATE_STORE"]
    store.save_packet(situate_packet(as_of="2026-08-01"))
    store.save_packet(situate_packet(as_of="2026-09-01"))
    latest = data_of(call(world, "situate_get", {"ticker": "nvda"}))
    assert latest["ticker"] == "NVDA" and latest["as_of"] == "2026-09-01"
    assert latest["one_line"] == "NVDA: the odds are balanced."
    assert latest["falsifiers"] == ["A close below the 200-day average."]
    assert len(latest["memo_excerpt"]) <= 1500
    older = data_of(call(world, "situate_get", {"ticker": "NVDA", "as_of": "2026-08-01"}))
    assert older["as_of"] == "2026-08-01"
    assert set(world.routes) == {("GET", "/api/situate/NVDA/summary")}


def test_prism_get_reads_the_stored_summary(world: World) -> None:
    world.app.config["PRISM_STORE"].save_packet(prism_packet())
    summary = data_of(call(world, "prism_get", {"ticker": "NVDA"}))
    assert summary["ticker"] == "NVDA" and summary["recommendation"]["action"] == "buy"
    assert summary["fair_value"] == 230.0
    assert world.routes == [("GET", "/api/prism/NVDA/summary")]


def test_the_stored_reads_have_no_build_path(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the builders replaced by ones that fail the test, a missing packet is an error."""

    def must_not_build(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a stored read reached a build")

    monkeypatch.setattr(situate_engine, "build_situate_packet", must_not_build)
    monkeypatch.setattr(prism_engine, "build_prism_packet", must_not_build)
    for name, label in (("situate_get", "Situate"), ("prism_get", "Prism")):
        result = call(world, name, {"ticker": "ZZZZ"})
        assert result["isError"] is True
        text = text_of(result)
        assert text.startswith(f"{name}: no stored {label} packet for ZZZZ")
        assert "does not build one" in text and "POST" not in text


def test_peer_forecast_reads_the_route_and_passes_no_as_of(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    real = tabular_routes.peer_forecast_for_ticker

    def spy(ticker: str, **kwargs: Any) -> dict[str, Any]:
        seen.append({"ticker": ticker, **kwargs})
        return real(ticker, **kwargs)

    monkeypatch.setattr(tabular_routes, "peer_forecast_for_ticker", spy)
    result = call(world, "peer_forecast", {"ticker": "nvda", "horizon": 6})
    body = data_of(result)
    assert body["available"] is True and body["ticker"] == "NVDA" and body["sector"] == SECTOR
    assert body["horizon_months"] == 6 and body["bucket"] in pf.BUCKETS
    assert set(body["probabilities"]) == set(pf.BUCKETS)
    assert body["peer_count"] == len(UNIVERSE) and body["peers"]
    assert set(body["peers"][0]) == {"symbol", "bucket", "expected_excess_return", "confidence"}
    assert body["disclaimer"].startswith("Research only")
    assert len(text_of(result)) <= cm.RESULT_BUDGET
    assert [(call_["ticker"], call_["horizon"], call_["as_of"]) for call_ in seen] == [
        ("NVDA", 6, None)
    ]
    assert world.routes == [("GET", "/api/tabular/peer-forecast/NVDA")]
    assert data_of(call(world, "peer_forecast", {"ticker": "AAPL"}))["horizon_months"] == 3


def test_peer_forecast_calls_for_one_sector_and_horizon_share_a_model_build(
    world: World,
) -> None:
    """The route caches a sector's forecast (a live and a backtest pass) for 12 hours, and the
    tool has no ``as_of`` with which to ask for another."""
    for ticker in ("NVDA", "AAPL", "MSFT"):
        assert data_of(call(world, "peer_forecast", {"ticker": ticker}))["available"] is True
    assert world.predictor.calls == 2
    call(world, "peer_forecast", {"ticker": "NVDA", "horizon": 6})  # another horizon: a new build
    assert world.predictor.calls == 4


def test_peer_forecast_says_why_it_is_unavailable(world: World) -> None:
    """The tool executor keeps only ``error`` from a failed route and drops the 503's reason."""
    world.app.config["TABULAR_PREDICTOR"] = RecordingPredictor(fail=True)
    down = call(world, "peer_forecast", {"ticker": "NVDA"})
    assert down["isError"] is True
    assert text_of(down) == "peer_forecast: unavailable: fake model is down (HTTP 503)"

    world.app.config["TABULAR_PREDICTOR"] = world.predictor
    outside = call(world, "peer_forecast", {"ticker": "ZZZZ"})
    assert "unavailable" in text_of(outside) and "curated sector universe" in text_of(outside)

    world.app.config["TABULAR_PREDICTOR"] = None
    off = call(world, "peer_forecast", {"ticker": "NVDA"})
    assert off["isError"] is True and "TABULAR_INFERENCE_URL" in text_of(off)


# --------------------------------------------------------------------------------------
# refusals: a bad ticker, an unknown argument, a wrong type; and nothing is reached
# --------------------------------------------------------------------------------------

REFUSALS = [
    ("sec_source_pack", {"ticker": "AA$PL"}, "unsupported characters"),
    ("sec_source_pack", {"ticker": ""}, "Ticker is required"),
    ("sec_source_pack", {}, "missing required argument(s): ticker"),
    ("sec_source_pack", {"ticker": ["AAPL"]}, "ticker must be a string"),
    ("sec_source_pack", {"ticker": 5}, "ticker must be a string"),
    ("sec_source_pack", {"ticker": "AAPL", "tickers": "AAPL,MSFT"}, "unknown argument(s): tickers"),
    ("sec_source_pack", {"ticker": "AAPL/../health"}, "unsupported characters"),
    ("sec_source_pack", {"ticker": "AAPL?x=1"}, "unsupported characters"),
    ("sec_source_pack", {"ticker": "É" * 3}, "unsupported characters"),
    ("chart_data", {"chart_type": "portfolio", "ticker": "AAPL"}, "chart_type must be one of"),
    ("chart_data", {"chart_type": "torque", "ticker": "AAPL"}, "chart_type must be one of"),
    ("chart_data", {"chart_type": "peer-forecast", "ticker": "AAPL"}, "chart_type must be one of"),
    ("chart_data", {"chart_type": "../health", "ticker": "AAPL"}, "chart_type must be one of"),
    ("chart_data", {"ticker": "AAPL"}, "missing required argument(s): chart_type"),
    ("chart_data", {"chart_type": "auction", "ticker": "AAPL,MSFT"}, "unsupported characters"),
    (
        "chart_data",
        {"chart_type": "auction", "ticker": "AAPL", "watchlist_url": "https://x.example/w"},
        "unknown argument(s): watchlist_url",
    ),
    (
        "chart_data",
        {"chart_type": "auction", "ticker": "AAPL", "tickers": "A,B", "max_results": 50},
        "unknown argument(s): max_results, tickers",
    ),
    ("chart_data", {"chart_type": "auction", "ticker": "AAPL", "period": "9y"}, "period must be"),
    (
        "chart_data",
        {"chart_type": "auction", "ticker": "AAPL", "interval": "5m"},
        "interval must be",
    ),
    ("chart_data", {"chart_type": "performance", "ticker": "AAPL", "month": 13}, "month must be"),
    ("chart_data", {"chart_type": "performance", "ticker": "AAPL", "month": True}, "month must be"),
    ("chart_data", {"chart_type": "performance", "ticker": "AAPL", "month": "3"}, "month must be"),
    ("torque_data", {"ticker": "AAPL", "period": "1y"}, "unknown argument(s): period"),
    ("torque_data", {"ticker": "a b"}, "unsupported characters"),
    ("moneyline_data", {"ticker": "AAPL", "expiry": "tomorrow"}, "expiry must be a date"),
    ("moneyline_data", {"ticker": "AAPL", "expiry": 20261016}, "expiry must be a date"),
    ("moneyline_data", {"ticker": "AAPL", "strike": 100}, "unknown argument(s): strike"),
    ("situate_get", {"ticker": "NVDA", "as_of": "9999-99-99"}, "as_of must be an ISO date"),
    ("situate_get", {"ticker": "NVDA", "as_of": ""}, "as_of must be a date"),
    ("situate_get", {"ticker": "NVDA", "as_of": 20260901}, "as_of must be a date"),
    ("situate_get", {"ticker": "N" * 17}, "at most 16 characters"),
    ("situate_get", {"ticker": "NVDA", "force": True}, "unknown argument(s): force"),
    ("situate_get", {"ticker": "NVDA", "include_memo": False}, "unknown argument(s)"),
    ("prism_get", {"ticker": "NV DA"}, "unsupported characters"),
    ("prism_get", {"ticker": "NVDA", "message": "hi"}, "unknown argument(s): message"),
    ("prism_get", {"ticker": "NVDA", "as_of": "yesterday"}, "as_of must be an ISO date"),
    ("peer_forecast", {"ticker": "NVDA", "as_of": "2026-08-31"}, "unknown argument(s): as_of"),
    ("peer_forecast", {"ticker": "NVDA", "horizon": 4}, "horizon must be one of"),
    ("peer_forecast", {"ticker": "NVDA", "horizon": True}, "horizon must be one of"),
    ("peer_forecast", {"ticker": "NVDA", "horizon": 3.0}, "horizon must be one of"),
    ("peer_forecast", {"ticker": "NVDA", "horizon": [3]}, "horizon must be one of"),
    ("peer_forecast", {"ticker": "N" * 17}, "at most 16 characters"),
    ("peer_forecast", {"ticker": "NV/DA"}, "unsupported characters"),
    ("health_check", {"anything": 1}, "unknown argument(s): anything"),
    ("provider_status", {"ticker": "AAPL"}, "unknown argument(s): ticker"),
    ("list_capabilities", {"group": "data"}, "this tool takes no arguments"),
]


@pytest.mark.parametrize(
    ("name", "arguments", "reason"),
    REFUSALS,
    ids=[f"{name}-{index}" for index, (name, _, _) in enumerate(REFUSALS)],
)
def test_a_bad_call_is_refused_and_reaches_nothing(
    world: World, name: str, arguments: dict[str, Any], reason: str
) -> None:
    result = call(world, name, arguments)
    assert result["isError"] is True
    text = text_of(result)
    assert text.startswith(f"{name}: "), text
    assert reason in text, text
    # the refusal happened before any route, provider or model was asked
    assert world.routes == []
    assert world.market.calls == [] and world.sec.calls == [] and world.predictor.calls == 0


def test_null_is_an_absent_argument_but_an_unknown_name_is_still_refused(world: World) -> None:
    assert data_of(call(world, "moneyline_data", {"ticker": "AAPL", "expiry": None}))
    result = call(world, "moneyline_data", {"ticker": "AAPL", "watchlist_url": None})
    assert result["isError"] is True and "unknown argument(s): watchlist_url" in text_of(result)


def test_arguments_that_are_not_an_object_are_read_as_none_given(world: World) -> None:
    payload = rpc(world.client, "tools/call", {"name": "sec_source_pack", "arguments": "AAPL"})
    assert payload["result"]["isError"] is True
    assert "missing required argument(s): ticker" in payload["result"]["content"][0]["text"]


# --------------------------------------------------------------------------------------
# what the tools reach, and what they say when a route fails
# --------------------------------------------------------------------------------------


def test_every_tool_reaches_only_its_own_cheap_read_route(world: World) -> None:
    world.app.config["SITUATE_STORE"].save_packet(situate_packet())
    world.app.config["PRISM_STORE"].save_packet(prism_packet())
    calls = {
        "list_capabilities": {},
        "health_check": {},
        "provider_status": {},
        "sec_source_pack": {"ticker": "AAPL"},
        "chart_data": {"chart_type": "flow-compass", "ticker": "AAPL"},
        "torque_data": {"ticker": "AAPL"},
        "moneyline_data": {"ticker": "AAPL"},
        "situate_get": {"ticker": "NVDA"},
        "prism_get": {"ticker": "NVDA"},
        "peer_forecast": {"ticker": "NVDA"},
    }
    assert set(calls) | {"ask_lattice_animals"} == set(cm.CONSTELLATION_TOOLS)
    for name, arguments in calls.items():
        assert not call(world, name, arguments).get("isError"), name
    assert world.routes == [
        ("GET", "/api/health"),
        ("GET", "/api/providers"),
        ("GET", "/api/sec/AAPL"),
        ("POST", "/api/data/charts/flow-compass"),
        ("POST", "/api/data/tools/torque"),
        ("POST", "/api/data/tools/moneyline"),
        ("GET", "/api/situate/NVDA/summary"),
        ("GET", "/api/prism/NVDA/summary"),
        ("GET", "/api/tabular/peer-forecast/NVDA"),
    ]
    reached = {path for _, path in world.routes}
    assert "/api/data/ticker-research" not in reached
    assert not {"/api/situate", "/api/prism", "/api/agent/chat", "/api/tools/fax"} & reached


def test_a_failing_route_is_an_error_result_that_says_why(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(*_args: Any, **_kwargs: Any) -> None:
        raise MarketDataError("no data for that ticker")

    monkeypatch.setattr(world.market, "get_history", failing)
    result = call(world, "chart_data", {"chart_type": "auction", "ticker": "AAPL"})
    assert result["isError"] is True
    assert text_of(result) == "chart_data: AAPL: no data for that ticker (HTTP 400)"


def test_an_unexpected_failure_is_logged_and_answered_without_detail(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("internal detail that must not reach a caller")

    monkeypatch.setattr(cm, "_fit", explode)
    result = call(world, "health_check")
    assert result["isError"] is True and text_of(result) == "health_check failed"
    assert "constellation tool health_check failed" in caplog.text


# --------------------------------------------------------------------------------------
# text that leaves the server
# --------------------------------------------------------------------------------------


def test_secrets_and_endpoints_do_not_leave_in_an_error(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MASSIVE_API_KEY", "massive-key-9f8e7d6c5b4a")
    monkeypatch.setenv("TABULAR_INFERENCE_TOKEN", "tabular-token-0011223344")

    def leaky(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(
            "GET https://api.massive.com/v2/aggs?apiKey=abc123def456 failed with "
            "massive-key-9f8e7d6c5b4a; HTTPSConnectionPool(host='team--tabular.modal.run', "
            "port=443): Max retries exceeded with url: /predict-secret-path (Caused by x)"
        )

    monkeypatch.setattr(world.market, "get_history", leaky)
    text = text_of(call(world, "chart_data", {"chart_type": "auction", "ticker": "AAPL"}))
    for secret in (
        "massive-key-9f8e7d6c5b4a",
        "abc123def456",
        "api.massive.com",
        "modal.run",
        "predict-secret-path",
    ):
        assert secret not in text, secret
    assert "[url]" in text and "host='[host]'" in text and "with url: [path]" in text


def test_secrets_are_scrubbed_from_a_result_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXA_API_KEY", "exa-secret-value-123456")
    text = cm._fit({"note": "the key is exa-secret-value-123456", "n": 1})
    assert "exa-secret-value-123456" not in text and "[redacted]" in text
    assert json.loads(text)["n"] == 1


def test_a_very_large_payload_still_comes_out_as_valid_json_within_the_budget() -> None:
    huge = {
        "series": [
            {"date": f"2025-01-{index % 28 + 1:02d}", "value": index * 1.123456789}
            for index in range(5000)
        ],
        "text": "x" * 100_000,
        "nested": {str(index): ["y" * 500] * 50 for index in range(60)},
    }
    text = cm._fit(huge)
    assert len(text) <= cm.RESULT_BUDGET
    assert isinstance(json.loads(text), dict)


def test_fit_rounds_floats_drops_non_finite_numbers_and_keeps_the_newest_points() -> None:
    payload = {
        "price": 187.42315678,
        "big": 12_345_678.9,
        "tiny": 0.000123456789,
        "nan": float("nan"),
        "inf": float("inf"),
        "flag": True,
        "series": cm._summarize(
            [{"date": f"2025-01-{day:02d}", "value": float(day)} for day in range(1, 21)], tail=8
        ),
    }
    shrunk = json.loads(cm._fit(payload))
    assert (shrunk["price"], shrunk["big"], shrunk["tiny"]) == (187.423, 12_345_679, 0.000123457)
    assert shrunk["nan"] is None and shrunk["inf"] is None and shrunk["flag"] is True
    assert shrunk["series"]["points"] == 20 and len(shrunk["series"]["last"]) == 8
    tight = cm._shrink(payload["series"], chars=40, items=3)
    assert [point["value"] for point in tight["last"]] == [18.0, 19.0, 20.0]  # newest kept


def test_summarize_walks_nested_series_and_leaves_tables_alone() -> None:
    points = [{"date": f"2025-02-{day:02d}", "value": day} for day in range(1, 11)]
    table = [{"month": month, "value": month} for month in range(1, 13)]
    out = cm._summarize({"series": {"close": points, "deep": {"ema": points}}, "rows": table}, 3)
    assert out["series"]["close"] == {
        "points": 10,
        "from": "2025-02-01",
        "to": "2025-02-10",
        "last": points[-3:],
    }
    assert out["series"]["deep"]["ema"]["points"] == 10
    assert out["rows"] == table


# --------------------------------------------------------------------------------------
# the hop rule, against a stub peer on 127.0.0.1
# --------------------------------------------------------------------------------------


class StubPeer:
    """An MCP peer that records every request it gets and answers like the hub."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                message = json.loads(raw or b"null")
                outer.requests.append(
                    {
                        "path": self.path,
                        "headers": {key.lower(): value for key, value in self.headers.items()},
                        "body": message,
                    }
                )
                if not isinstance(message, dict) or "id" not in message:
                    self.send_response(202)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                if message.get("method") == "tools/list":
                    result: dict[str, Any] = {
                        "tools": [
                            {
                                "name": "ask_the_minds",
                                "description": "ask",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"question": {}},
                                    "required": ["question"],
                                },
                            }
                        ]
                    }
                else:
                    result = {"content": [{"type": "text", "text": "the minds say hello"}]}
                reply = {"jsonrpc": "2.0", "id": message["id"], "result": result}
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:
                del format, args  # keep the test output quiet

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def peer() -> Iterator[StubPeer]:
    stub = StubPeer()
    yield stub
    stub.close()


@pytest.fixture()
def hop_world(peer: StubPeer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> World:
    monkeypatch.setenv("LATTICE_MCP_URL", peer.url)
    monkeypatch.setenv("MCP_ALLOW_LOCAL", "1")
    return make_world(tmp_path)


ASK = {"question": "what do you make of NVDA?"}


def test_at_hop_zero_the_call_goes_out_one_hop_deeper_with_this_sites_name(
    hop_world: World, peer: StubPeer
) -> None:
    result = call(hop_world, "ask_lattice_animals", ASK)
    assert text_of(result) == "the minds say hello" and not result.get("isError")
    assert result["_meta"]["constellation"] == {
        "server": "underlying-analyzer",
        "hop": 0,
        "path": ["underlying-analyzer"],
    }
    (outbound,) = peer.requests
    assert outbound["path"] == "/mcp"
    assert outbound["headers"]["x-mcp-hop"] == "1"
    assert outbound["headers"]["x-lattice-hop"] == "1"
    assert outbound["headers"]["x-mcp-path"] == "underlying-analyzer"
    assert outbound["body"]["method"] == "tools/call"
    assert outbound["body"]["params"] == {"name": "ask_the_minds", "arguments": ASK}


def test_one_hop_in_the_path_grows_and_the_next_call_is_hop_two(
    hop_world: World, peer: StubPeer
) -> None:
    headers = {"x-mcp-hop": "1", "x-mcp-path": "lattice"}
    call(hop_world, "ask_lattice_animals", {**ASK, "to": "app"}, headers=headers)
    (outbound,) = peer.requests
    assert outbound["headers"]["x-mcp-hop"] == "2"
    assert outbound["headers"]["x-mcp-path"] == "lattice>underlying-analyzer"
    assert outbound["body"]["params"]["arguments"] == {**ASK, "to": "app"}


@pytest.mark.parametrize("header", ["x-mcp-hop", "x-lattice-hop"])
def test_at_the_limit_the_lattice_tool_is_refused_and_nothing_goes_out(
    hop_world: World, peer: StubPeer, header: str
) -> None:
    result = call(hop_world, "ask_lattice_animals", ASK, headers={header: str(MAX_HOP)})
    assert result["isError"] is True
    assert text_of(result).startswith("too-deep")
    assert peer.requests == []


def test_at_the_limit_a_pure_tool_still_answers(hop_world: World, peer: StubPeer) -> None:
    result = call(hop_world, "health_check", headers={"x-mcp-hop": str(MAX_HOP)})
    assert data_of(result)["ok"] is True
    assert result["_meta"]["constellation"]["hop"] == MAX_HOP
    assert peer.requests == []


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        ({}, "ask something: a question of 1 to 600 characters"),
        ({"question": ""}, "ask something: a question of 1 to 600 characters"),
        ({"question": "   "}, "ask something: a question of 1 to 600 characters"),
        ({"question": ["a"]}, "ask something: a question of 1 to 600 characters"),
        ({"question": "hi", "to": "nobody"}, "to must be field, app or connectome"),
    ],
)
def test_a_bad_lattice_question_is_refused_before_anything_goes_out(
    hop_world: World, peer: StubPeer, arguments: dict[str, Any], reason: str
) -> None:
    """The library's own check (v2), which runs before the call goes out."""
    result = call(hop_world, "ask_lattice_animals", arguments)
    assert result["isError"] is True and text_of(result) == reason
    assert peer.requests == []


def test_a_good_lattice_question_may_name_who_answers(hop_world: World, peer: StubPeer) -> None:
    call(hop_world, "ask_lattice_animals", {"question": "hi", "to": "connectome"})
    assert peer.requests[0]["body"]["params"]["arguments"] == {"question": "hi", "to": "connectome"}


def test_the_lattice_tool_forwards_a_question_and_who_answers_and_nothing_else(
    hop_world: World, peer: StubPeer
) -> None:
    call(
        hop_world,
        "ask_lattice_animals",
        {"question": "hi", "url": "http://evil.example/mcp", "peer": "elsewhere", "n": 1},
    )
    assert peer.requests[0]["body"]["params"]["arguments"] == {"question": "hi"}
    assert [request["path"] for request in peer.requests] == ["/mcp"]


def test_a_long_lattice_question_is_cut_to_600_characters(hop_world: World, peer: StubPeer) -> None:
    call(hop_world, "ask_lattice_animals", {"question": "x" * 900})
    assert len(peer.requests[0]["body"]["params"]["arguments"]["question"]) == 600


def test_the_lattice_tool_asks_for_its_own_arguments(hop_world: World, peer: StubPeer) -> None:
    payload = rpc(
        hop_world.client,
        "tools/list",
    )["result"]["tools"][-1]
    assert payload["name"] == "ask_lattice_animals"
    assert payload["inputSchema"]["required"] == ["question"]
    assert payload["annotations"] == {"readOnlyHint": True, "openWorldHint": True}
    assert peer.requests == []


def test_the_relay_path_forwards_the_hubs_own_mcp_one_hop_deeper(
    hop_world: World, peer: StubPeer
) -> None:
    payload = rpc(hop_world.client, "tools/list", path="/mcp/lattice")
    assert payload["result"]["tools"][0]["name"] == "ask_the_minds"
    (outbound,) = peer.requests
    assert outbound["headers"]["x-mcp-hop"] == "1"
    assert outbound["headers"]["x-mcp-path"] == "underlying-analyzer"
    assert outbound["body"]["method"] == "tools/list"


def test_the_relay_path_at_the_limit_is_an_error_and_goes_nowhere(
    hop_world: World, peer: StubPeer
) -> None:
    payload = rpc(
        hop_world.client, "tools/list", path="/mcp/lattice", headers={"x-mcp-hop": str(MAX_HOP)}
    )
    assert payload["error"]["code"] == -32001
    assert peer.requests == []


def test_the_relay_path_only_serves_a_name_in_the_registry(
    hop_world: World, peer: StubPeer
) -> None:
    response = hop_world.client.post(
        "/mcp/reflect-search", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert response.status_code == 404 and response.get_json()["error"]["code"] == -32000
    assert peer.requests == []


def test_a_plain_http_hub_is_not_reached_unless_the_operator_allows_local(
    peer: StubPeer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LATTICE_MCP_URL", peer.url)  # MCP_ALLOW_LOCAL is not set
    strict = make_world(tmp_path)
    result = call(strict, "ask_lattice_animals", ASK)
    assert result["isError"] is True
    assert text_of(result) == "the lattice animals did not answer (not-configured)"
    assert peer.requests == []


def test_the_per_address_limit_is_the_libraries_and_uses_the_last_forwarded_address(
    world: World,
) -> None:
    headers = {"X-Forwarded-For": "203.0.113.7, 198.51.100.9"}
    outcomes = [call(world, "health_check", headers=headers).get("isError") for _ in range(41)]
    assert outcomes[:40] == [None] * 40 and outcomes[40] is True
    # a different last address has its own budget
    other = {"X-Forwarded-For": "203.0.113.7, 198.51.100.10"}
    assert not call(world, "health_check", headers=other).get("isError")
