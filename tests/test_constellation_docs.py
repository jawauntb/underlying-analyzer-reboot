"""Where the constellation MCP is documented: the route catalogs, docs/mcp.md and .env.example.

The behavior is in ``test_constellation_mcp.py``. These tests keep the places that describe it
from drifting from the code: the tool list, the routes, the count of registry tools, and the
environment variables.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from flask.testing import FlaskClient

from app import constellation_mcp as cm
from app.main import create_app
from app.tool_registry import TOOLS

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> FlaskClient:
    monkeypatch.setenv("UNDERLYING_SKIP_DOTENV", "1")
    return create_app().test_client()


def test_the_routes_are_in_the_route_catalogs(client: FlaskClient) -> None:
    docs = client.get("/api/docs").get_json()
    endpoints = {(row["method"], row["path"]) for row in docs["endpoints"]}
    routes = {("POST", "/mcp"), ("POST", "/mcp/{peer}"), ("GET", "/.well-known/mcp.json")}
    assert routes <= endpoints
    assert docs["mcp"]["endpoint"] == "/api/mcp"  # the registry's MCP, unchanged
    assert docs["mcp"]["constellation"]["endpoint"] == "/mcp"
    assert docs["mcp"]["constellation"]["tools"] == list(cm.CONSTELLATION_TOOLS)

    openapi = client.get("/api/openapi").get_json()
    assert "post" in openapi["paths"]["/mcp"] and "post" in openapi["paths"]["/mcp/{peer}"]
    assert "get" in openapi["paths"]["/.well-known/mcp.json"]
    assert openapi["paths"]["/mcp/{peer}"]["post"]["parameters"][0]["name"] == "peer"
    assert openapi["x-mcp"]["endpoint"] == "/api/mcp"  # unchanged
    assert openapi["x-mcp-constellation"]["endpoint"] == "/mcp"
    assert openapi["x-mcp-constellation"]["tools"] == list(cm.CONSTELLATION_TOOLS)


def test_the_mcp_page_names_every_registry_tool_and_every_constellation_tool() -> None:
    page = (ROOT / "docs" / "mcp.md").read_text()
    for spec in TOOLS:
        assert f"`{spec.name}`" in page, f"docs/mcp.md does not name {spec.name}"
    for name in cm.CONSTELLATION_TOOLS:
        assert f"`{name}`" in page, f"docs/mcp.md does not name {name}"
    assert f"The registry has {len(TOOLS)} tools" in page


def test_the_environment_variables_are_documented_where_they_are_set_and_read() -> None:
    page = (ROOT / "docs" / "mcp.md").read_text()
    env_example = (ROOT / ".env.example").read_text()
    for variable in (
        "LATTICE_MCP_URL",
        "MCP_PUBLIC_ORIGIN",
        "MCP_ALLOW_LOCAL",
        "MCP_PEER_FORECAST_DAILY_CAP",
    ):
        assert variable in page, variable
        assert variable in env_example, variable
        assert variable in (ROOT / "app" / "constellation_mcp.py").read_text(), variable


def test_the_docs_say_the_limits_are_per_process_and_gunicorn_runs_three_workers() -> None:
    page = (ROOT / "docs" / "mcp.md").read_text()
    assert "--workers 3" in (ROOT / "Procfile").read_text()
    assert "three gunicorn workers" in page and "up to three times" in page
    assert "REST route" in page and "not capped" in page
