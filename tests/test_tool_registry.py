from __future__ import annotations

import pytest
from flask import Flask

from app.main import create_app
from app.tool_executor import ToolArtifact, _compact, _extract_artifacts
from app.tool_registry import (
    TOOLS,
    ToolArgumentError,
    anthropic_tool_definitions,
    build_request,
    coerce_arguments,
    get_tool,
    mcp_tool_definitions,
    tool_catalog_payload,
)


@pytest.fixture()
def app() -> Flask:
    return create_app()


def test_tool_names_are_unique_and_valid() -> None:
    names = [spec.name for spec in TOOLS]
    assert len(names) == len(set(names))
    for name in names:
        assert name.replace("_", "").isalnum(), name


def test_every_tool_binds_to_a_registered_route(app: Flask) -> None:
    """The registry may only point at routes the app actually serves."""
    registered = {
        (rule.rule, method)
        for rule in app.url_map.iter_rules()
        for method in rule.methods or set()
    }

    for spec in TOOLS:
        flask_path = spec.path
        for param in spec.path_params:
            flask_path = flask_path.replace("{" + param + "}", f"<{param}>")
        assert (flask_path, spec.method) in registered, f"{spec.name} -> {spec.path}"


def test_path_params_are_declared_in_schema() -> None:
    for spec in TOOLS:
        for param in spec.path_params:
            assert param in spec.properties, f"{spec.name}.{param}"
            assert param in spec.required, f"{spec.name}.{param} must be required"


def test_schemas_are_well_formed() -> None:
    for spec in TOOLS:
        assert spec.input_schema["type"] == "object"
        for key, prop in spec.properties.items():
            assert "type" in prop, f"{spec.name}.{key} has no type"
            assert "description" in prop or "enum" in prop, f"{spec.name}.{key}"


def test_anthropic_and_mcp_definitions_agree() -> None:
    anthropic = {tool["name"] for tool in anthropic_tool_definitions()}
    mcp = {tool["name"] for tool in mcp_tool_definitions()}
    assert anthropic == mcp
    assert "render_chart" in anthropic


def test_catalog_payload_covers_every_tool() -> None:
    catalog = tool_catalog_payload()
    assert catalog["tool_count"] == len(TOOLS)
    grouped = {name for group in catalog["groups"] for name in group["tools"]}
    assert grouped == {spec.name for spec in TOOLS}


def test_build_request_fills_path_and_body() -> None:
    spec = get_tool("render_chart")
    method, path, body, query = build_request(
        spec, {"chart_type": "auction", "ticker": "aapl", "period": "6mo"}
    )
    assert method == "POST"
    assert path == "/api/charts/auction"
    assert body == {"ticker": "aapl", "period": "6mo"}
    assert query == {}


def test_build_request_uses_query_for_get_tools() -> None:
    spec = get_tool("analyze_ticker")
    method, path, body, query = build_request(spec, {"ticker": "MSFT"})
    assert (method, path, body, query) == ("GET", "/api/analysis/MSFT", None, {})


def test_build_request_rejects_unknown_enum_value() -> None:
    with pytest.raises(ToolArgumentError, match="must be one of"):
        build_request(get_tool("render_chart"), {"chart_type": "candlestick"})


def test_missing_required_argument_is_reported() -> None:
    with pytest.raises(ToolArgumentError, match="missing required"):
        coerce_arguments(get_tool("stock_fax"), {})


def test_coerce_drops_unknown_keys_and_casts_scalars() -> None:
    cleaned = coerce_arguments(
        get_tool("analyze_batch"),
        {"tickers": "AAPL, MSFT", "max_results": "5", "nonsense": True},
    )
    assert cleaned == {"tickers": "AAPL, MSFT", "max_results": 5}


def test_unknown_tool_lists_alternatives() -> None:
    with pytest.raises(ToolArgumentError, match="Available tools"):
        get_tool("teleport")


def test_extract_artifacts_lifts_base64_out_of_results() -> None:
    spec = get_tool("render_chart")
    artifacts: list[ToolArtifact] = []
    payload = {
        "images": [
            {
                "filename": "auction.png",
                "mime": "image/png",
                "data": "A" * 900,
                "meta": {"title": "Auction levels", "caption": "AAPL 6mo"},
            }
        ],
        "meta": {"state": "LONG OK"},
    }

    stripped = _extract_artifacts(payload, artifacts, spec)

    assert len(artifacts) == 1
    assert artifacts[0].id == "img_1"
    assert artifacts[0].data == "A" * 900
    assert artifacts[0].title == "Auction levels"
    assert stripped["images"][0]["image_ref"] == "img_1"
    assert "data" not in stripped["images"][0]
    assert stripped["meta"]["state"] == "LONG OK"


def test_compact_trims_long_strings_and_arrays() -> None:
    compacted = _compact({"note": "x" * 5000, "rows": list(range(100))})
    assert len(compacted["note"]) < 5000
    assert "chars total" in compacted["note"]
    assert len(compacted["rows"]) == 41
    assert "more items omitted" in compacted["rows"][-1]
