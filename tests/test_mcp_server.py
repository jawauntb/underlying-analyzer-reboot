from __future__ import annotations

from mcp_server.server import CHART_TYPES, _strip_heavy_fields, call_tool, list_tools


def test_strip_heavy_fields_omits_base64() -> None:
    payload = {
        "images": [{"filename": "a.png", "mime": "image/png", "data": "x" * 500}],
        "meta": {"state": "LONG OK"},
        "nested": {"image": "y" * 300},
    }

    cleaned = _strip_heavy_fields(payload)

    assert cleaned["meta"]["state"] == "LONG OK"
    assert "omitted base64" in cleaned["images"][0]["data"]
    assert "omitted base64" in cleaned["nested"]["image"]


def test_strip_heavy_fields_keeps_short_values() -> None:
    assert _strip_heavy_fields({"data": "short"}) == {"data": "short"}


def test_tools_are_generated_from_the_registry() -> None:
    tools = list_tools()
    names = {tool["name"] for tool in tools}

    assert {"render_chart", "analyze_ticker", "list_capabilities"} <= names
    for tool in tools:
        assert tool["inputSchema"]["properties"]["include_images"]["type"] == "boolean"


def test_include_images_switch_does_not_leak_into_the_registry() -> None:
    from app.tool_registry import get_tool

    list_tools()
    assert "include_images" not in get_tool("render_chart").properties


def test_call_tool_rejects_unknown_chart_type_before_any_request() -> None:
    result = call_tool("render_chart", {"chart_type": "not-a-chart", "ticker": "AAPL"})

    assert result["ok"] is False
    assert "must be one of" in result["error"]
    assert "auction" in result["error"]
    assert "ridge-growth" in CHART_TYPES


def test_call_tool_rejects_unknown_tool_name() -> None:
    result = call_tool("teleport", {})

    assert result["ok"] is False
    assert "Available tools" in result["error"]
