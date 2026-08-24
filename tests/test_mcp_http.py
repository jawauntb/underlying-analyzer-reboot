from __future__ import annotations

import json
from typing import Any

import pytest
from flask.testing import FlaskClient
from werkzeug.test import TestResponse

from app.main import create_app
from app.mcp_http import METHOD_NOT_FOUND, PROTOCOL_VERSION, handle_mcp_payload
from app.tool_executor import ToolResult


@pytest.fixture()
def client() -> FlaskClient:
    app = create_app()
    return app.test_client()


def rpc(
    client: FlaskClient,
    method: str,
    params: dict[str, Any] | None = None,
    request_id: int = 1,
) -> TestResponse:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/api/mcp", json=body)


def test_descriptor_is_served_on_get(client: FlaskClient) -> None:
    payload = client.get("/api/mcp").get_json()
    assert payload["transport"] == "streamable-http"
    assert payload["endpoint"] == "/api/mcp"
    assert payload["tool_count"] > 0
    assert payload["authentication"] == "none"


def test_initialize_negotiates_protocol(client: FlaskClient) -> None:
    result = rpc(client, "initialize", {"protocolVersion": "2024-11-05"}).get_json()["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "underlying-analyzer"
    assert "tools" in result["capabilities"]


def test_initialize_falls_back_for_unknown_version(client: FlaskClient) -> None:
    result = rpc(client, "initialize", {"protocolVersion": "1999-01-01"}).get_json()["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION


def test_tools_list_exposes_schemas(client: FlaskClient) -> None:
    tools = rpc(client, "tools/list").get_json()["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert {"render_chart", "analyze_ticker", "compose_research_article"} <= names
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"]


def test_tools_call_runs_a_real_tool(client: FlaskClient) -> None:
    result = rpc(client, "tools/call", {"name": "list_capabilities", "arguments": {}}).get_json()[
        "result"
    ]

    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["result"]["tool_count"] > 0


def test_mcp_uses_the_full_view_only_for_ticker_research(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_execute(name: str, _arguments: dict[str, Any], **kwargs: Any) -> ToolResult:
        calls.append((name, kwargs["result_view"]))
        return ToolResult(
            name=name,
            ok=True,
            status=200,
            url="/api/fake",
            result={"payload": "x" * 20_000},
        )

    monkeypatch.setattr("app.mcp_http.execute_tool", fake_execute)
    regular = handle_mcp_payload(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "health_check", "arguments": {}},
        }
    )
    packet = handle_mcp_payload(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "ticker_research_bundle",
                "arguments": {"ticker": "AAPL"},
            },
        }
    )

    assert calls == [("health_check", "agent"), ("ticker_research_bundle", "full")]
    regular_text = json.loads(regular["result"]["content"][0]["text"])
    packet_text = json.loads(packet["result"]["content"][0]["text"])
    assert regular_text["truncated"] is True
    assert packet_text["result"]["payload"] == "x" * 20_000


def test_tools_call_reports_validation_errors_as_invalid_params(client: FlaskClient) -> None:
    response = rpc(client, "tools/call", {"name": "stock_fax", "arguments": {}}).get_json()
    assert response["error"]["code"] == -32602
    assert "missing required" in response["error"]["message"]


def test_tools_call_composes_an_article(client: FlaskClient) -> None:
    result = rpc(
        client,
        "tools/call",
        {
            "name": "compose_research_article",
            "arguments": {
                "title": "Semis into the print",
                "thesis": "Positioning is crowded but the trend is intact.",
                "sections": [{"heading": "Setup", "body": "Price is above value."}],
                "recommendations": [
                    {
                        "ticker": "nvda",
                        "stance": "constructive",
                        "action": "Watch for acceptance above VAH",
                        "invalidation": "Loses the value area low",
                    }
                ],
            },
        },
    ).get_json()["result"]

    payload = json.loads(result["content"][0]["text"])["result"]
    assert payload["article"]["recommendations"][0]["ticker"] == "NVDA"
    assert "## Recommendations" in payload["markdown"]


def test_unknown_method_returns_method_not_found(client: FlaskClient) -> None:
    response = rpc(client, "tools/teleport").get_json()
    assert response["error"]["code"] == METHOD_NOT_FOUND


def test_notifications_get_no_response_body(client: FlaskClient) -> None:
    response = client.post(
        "/api/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert response.status_code == 202
    assert not response.get_data()


def test_batch_requests_are_answered_together(client: FlaskClient) -> None:
    response = client.post(
        "/api/mcp",
        json=[
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ],
    ).get_json()

    assert [item["id"] for item in response] == [1, 2]


def test_sse_accept_header_returns_event_stream(client: FlaskClient) -> None:
    response = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Accept": "text/event-stream"},
    )
    assert response.mimetype == "text/event-stream"
    assert response.get_data(as_text=True).startswith("event: message\ndata: ")


def test_malformed_body_is_a_parse_error(client: FlaskClient) -> None:
    response = client.post("/api/mcp", data="not json", content_type="application/json")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == -32700


def test_resources_round_trip(client: FlaskClient) -> None:
    resources = rpc(client, "resources/list").get_json()["result"]["resources"]
    uris = {resource["uri"] for resource in resources}
    assert "underlying://catalog/tools" in uris

    read = rpc(client, "resources/read", {"uri": "underlying://catalog/tools"}).get_json()["result"]
    assert json.loads(read["contents"][0]["text"])["tool_count"] > 0


def test_handler_rejects_non_object_payloads() -> None:
    assert handle_mcp_payload("nope")["error"]["code"] == -32600
