from __future__ import annotations

from app.main import create_app
from app.openapi import build_openapi_document
from app.tool_registry import TOOLS


def test_document_covers_every_tool() -> None:
    document = build_openapi_document()
    operations = {
        operation["operationId"]
        for path in document["paths"].values()
        for operation in path.values()
    }
    assert {spec.name for spec in TOOLS} <= operations


def test_path_parameters_are_declared() -> None:
    operation = build_openapi_document()["paths"]["/api/analysis/{ticker}"]["get"]
    names = {parameter["name"] for parameter in operation["parameters"]}
    assert "ticker" in names
    assert all(parameter["in"] == "path" for parameter in operation["parameters"])


def test_post_tools_declare_a_request_body() -> None:
    operation = build_openapi_document()["paths"]["/api/charts/{chart_type}"]["post"]
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert "ticker" in schema["properties"]
    assert "chart_type" not in schema["properties"]


def test_supporting_routes_are_documented() -> None:
    paths = build_openapi_document()["paths"]
    assert "post" in paths["/api/mcp"]
    assert "post" in paths["/api/agent/chat/stream"]
    assert "get" in paths["/api/config"]


def test_security_search_documents_query_boundaries_and_result_contract() -> None:
    operation = build_openapi_document()["paths"]["/api/data/search"]["get"]
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}

    assert parameters["q"]["required"] is True
    assert parameters["q"]["schema"] == {"type": "string", "minLength": 1, "maxLength": 100}
    assert parameters["limit"]["required"] is False
    assert parameters["limit"]["schema"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 10,
        "default": 8,
    }

    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["required"] == ["query", "results", "provider"]
    assert schema["properties"]["results"]["items"]["required"] == [
        "symbol",
        "name",
        "exchange",
        "asset_type",
    ]
    assert schema["properties"]["results"]["items"]["properties"]["symbol"] == {
        "type": "string",
        "maxLength": 32,
        "pattern": r"^(?:[A-Z0-9][A-Z0-9.-]{0,31}|\^[A-Z0-9][A-Z0-9.-]{0,30})$",
    }
    assert operation["responses"]["502"]["description"] == "Market data provider unavailable"
    assert operation["responses"]["503"]["description"] == "Security search capacity is busy"


def test_agent_routes_publish_the_exact_policy_request_contract() -> None:
    paths = build_openapi_document()["paths"]
    for path in ("/api/agent/chat", "/api/agent/chat/stream"):
        schema = paths[path]["post"]["requestBody"]["content"]["application/json"][
            "schema"
        ]
        assert schema["required"] == ["messages"]
        assert schema["properties"]["tool_policy"]["enum"] == ["exact"]
        assert schema["properties"]["tools"]["items"]["type"] == "string"

    stream_content = paths["/api/agent/chat/stream"]["post"]["responses"]["200"][
        "content"
    ]
    assert "application/x-ndjson" in stream_content


def test_served_document_includes_this_deployment() -> None:
    payload = create_app().test_client().get("/api/openapi").get_json()
    assert payload["openapi"] == "3.1.0"
    assert payload["servers"][0]["url"].startswith("http")
    assert payload["x-mcp"]["endpoint"] == "/api/mcp"


def test_docs_catalog_advertises_the_new_surfaces() -> None:
    payload = create_app().test_client().get("/api/docs").get_json()
    assert payload["mcp"]["endpoint"] == "/api/mcp"
    assert payload["docs"]["openapi"] == "/api/openapi"
    assert payload["agent"]["chat"] == "/chat"
    assert len(payload["tools"]) == len(TOOLS)
    assert any(tool["id"] == "chat" for tool in payload["site_tools"])
    agent_chat = next(
        endpoint
        for endpoint in payload["endpoints"]
        if endpoint["path"] == "/api/agent/chat"
    )
    assert agent_chat["body"]["tool_policy"] == "'exact'?"
