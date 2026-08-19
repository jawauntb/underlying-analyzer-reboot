"""OpenAPI 3.1 document generated from the tool registry and route catalog."""

from __future__ import annotations

from typing import Any

from app.market_data import (
    MAX_SEARCH_QUERY_LENGTH,
    MAX_SECURITY_SYMBOL_LENGTH,
    SEARCH_PROVIDER,
    SECURITY_SYMBOL_PATTERN,
)
from app.tool_registry import GROUPS, TOOLS, ToolSpec

API_TITLE = "The Underlying Analyzer API"
API_VERSION = "1.0.0"
API_DESCRIPTION = (
    "Chart-led market research API. Every endpoint is publicly callable "
    "without an API key. The same capabilities are exposed as agent tools "
    "over MCP at POST /api/mcp and inside the product at /chat."
)

AGENT_REQUEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "messages": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "role": {"type": "string", "enum": ["user", "assistant"]},
                    "content": {"type": "string"},
                },
                "required": ["role", "content"],
            },
        },
        "tools": {"type": "array", "items": {"type": "string"}},
        "tool_policy": {"type": "string", "enum": ["exact"]},
        "context": {"type": "string"},
    },
    "required": ["messages"],
}

# Routes that exist for humans and infrastructure rather than as agent tools.
SUPPORTING_ROUTES: tuple[dict[str, Any], ...] = (
    {
        "method": "GET",
        "path": "/api/data/search",
        "tag": "data",
        "summary": "Look up securities by ticker or company name",
        "parameters": [
            {
                "name": "q",
                "in": "query",
                "required": True,
                "schema": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_SEARCH_QUERY_LENGTH,
                },
                "description": "Ticker symbol or company name",
            },
            {
                "name": "limit",
                "in": "query",
                "required": False,
                "schema": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 8,
                },
                "description": "Maximum number of results",
            },
        ],
        "success_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "symbol": {
                                "type": "string",
                                "maxLength": MAX_SECURITY_SYMBOL_LENGTH,
                                "pattern": SECURITY_SYMBOL_PATTERN,
                            },
                            "name": {"type": "string"},
                            "exchange": {"type": "string"},
                            "asset_type": {
                                "type": "string",
                                "enum": ["equity", "etf", "mutual_fund", "index", "crypto"],
                            },
                        },
                        "required": ["symbol", "name", "exchange", "asset_type"],
                        "additionalProperties": False,
                    },
                },
                "provider": {"type": "string", "const": SEARCH_PROVIDER},
            },
            "required": ["query", "results", "provider"],
            "additionalProperties": False,
        },
        "error_responses": {
            "502": "Market data provider unavailable",
            "503": "Security search capacity is busy",
        },
    },
    {
        "method": "GET",
        "path": "/api/config",
        "tag": "meta",
        "summary": "Public Supabase client configuration",
    },
    {
        "method": "GET",
        "path": "/api/data/market/snapshot",
        "tag": "data",
        "summary": "Massive stock snapshot",
        "parameters": [
            {"name": "ticker", "in": "query", "required": True, "schema": {"type": "string"}},
            {
                "name": "asset_class",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "default": "stocks"},
            },
        ],
    },
    {
        "method": "GET",
        "path": "/api/capabilities",
        "tag": "meta",
        "summary": "Massive dataset availability and freshness hints",
    },
    {
        "method": "GET",
        "path": "/api/capabilities/{capability_id}",
        "tag": "meta",
        "summary": "One Massive capability availability record",
        "parameters": [
            {"name": "capability_id", "in": "path", "required": True, "schema": {"type": "string"}}
        ],
    },
    {
        "method": "GET",
        "path": "/api/data/market/aggregates/{ticker}",
        "tag": "data",
        "summary": "Massive custom aggregate bars",
        "parameters": [
            {"name": "ticker", "in": "path", "required": True, "schema": {"type": "string"}},
            {
                "name": "start",
                "in": "query",
                "required": True,
                "schema": {"type": "string", "format": "date"},
            },
            {
                "name": "end",
                "in": "query",
                "required": True,
                "schema": {"type": "string", "format": "date"},
            },
            {
                "name": "multiplier",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "default": 1},
            },
            {
                "name": "timespan",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "default": "day"},
            },
            {
                "name": "asset_class",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "default": "stocks"},
            },
        ],
    },
    {
        "method": "GET",
        "path": "/api/data/market/trades/{ticker}",
        "tag": "data",
        "summary": "Massive historical trades",
        "parameters": [
            {"name": "ticker", "in": "path", "required": True, "schema": {"type": "string"}}
        ],
    },
    {
        "method": "GET",
        "path": "/api/data/market/quotes/{ticker}",
        "tag": "data",
        "summary": "Massive historical quotes",
        "parameters": [
            {"name": "ticker", "in": "path", "required": True, "schema": {"type": "string"}}
        ],
    },
    {
        "method": "GET",
        "path": "/api/data/options/{ticker}/chain",
        "tag": "data",
        "summary": "Massive option chain snapshot",
        "parameters": [
            {"name": "ticker", "in": "path", "required": True, "schema": {"type": "string"}},
            {
                "name": "expiry",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "format": "date"},
            },
        ],
    },
    {
        "method": "GET",
        "path": "/api/data/options/{ticker}/expirations",
        "tag": "data",
        "summary": "Massive option expiration dates",
        "parameters": [
            {"name": "ticker", "in": "path", "required": True, "schema": {"type": "string"}}
        ],
    },
    {
        "method": "GET",
        "path": "/api/data/options/{ticker}/contracts",
        "tag": "data",
        "summary": "Massive option contract reference data",
        "parameters": [
            {"name": "ticker", "in": "path", "required": True, "schema": {"type": "string"}}
        ],
    },
    {
        "method": "GET",
        "path": "/api/data/market/events/{ticker}",
        "tag": "data",
        "summary": "Massive ticker events",
        "parameters": [
            {"name": "ticker", "in": "path", "required": True, "schema": {"type": "string"}}
        ],
    },
    {
        "method": "GET",
        "path": "/api/data/market/dividends/{ticker}",
        "tag": "data",
        "summary": "Massive stock dividend events",
        "parameters": [
            {"name": "ticker", "in": "path", "required": True, "schema": {"type": "string"}}
        ],
    },
    {
        "method": "GET",
        "path": "/api/data/market/splits/{ticker}",
        "tag": "data",
        "summary": "Massive stock split events",
        "parameters": [
            {"name": "ticker", "in": "path", "required": True, "schema": {"type": "string"}}
        ],
    },
    {
        "method": "GET",
        "path": "/api/data/market/financials/{ticker}/{statement}",
        "tag": "data",
        "summary": "Massive financial statements and ratios",
        "parameters": [
            {"name": "ticker", "in": "path", "required": True, "schema": {"type": "string"}},
            {"name": "statement", "in": "path", "required": True, "schema": {"type": "string"}},
            {
                "name": "period_end",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "format": "date"},
            },
            {"name": "timeframe", "in": "query", "required": False, "schema": {"type": "string"}},
        ],
    },
    {
        "method": "GET",
        "path": "/api/data/market/status",
        "tag": "data",
        "summary": "Massive current US market status",
    },
    {
        "method": "GET",
        "path": "/api/data/market/news/{ticker}",
        "tag": "data",
        "summary": "Massive ticker news",
        "parameters": [
            {"name": "ticker", "in": "path", "required": True, "schema": {"type": "string"}},
            {"name": "limit", "in": "query", "required": False, "schema": {"type": "integer", "default": 20}},
        ],
    },
    {
        "method": "GET",
        "path": "/api/data/market/corporate-events",
        "tag": "data",
        "summary": "TMX corporate events",
        "parameters": [
            {"name": "ticker", "in": "query", "required": False, "schema": {"type": "string"}},
            {"name": "date", "in": "query", "required": False, "schema": {"type": "string", "format": "date"}},
            {"name": "type", "in": "query", "required": False, "schema": {"type": "string"}},
        ],
    },
    {
        "method": "GET",
        "path": "/api/data/market/ipos",
        "tag": "data",
        "summary": "Massive IPO events",
    },
    {
        "method": "GET",
        "path": "/api/data/market/conditions",
        "tag": "data",
        "summary": "Massive trade and quote conditions",
    },
    {
        "method": "GET",
        "path": "/api/data/market/snapshot/all",
        "tag": "data",
        "summary": "Massive full-market stock snapshot",
    },
    {
        "method": "GET",
        "path": "/api/data/options/{ticker}/snapshot/{contract}",
        "tag": "data",
        "summary": "Massive option contract snapshot",
        "parameters": [
            {"name": "ticker", "in": "path", "required": True, "schema": {"type": "string"}},
            {"name": "contract", "in": "path", "required": True, "schema": {"type": "string"}},
        ],
    },
    {
        "method": "GET",
        "path": "/api/data/market/stream",
        "tag": "data",
        "summary": "Massive WebSocket market events as Server-Sent Events",
        "parameters": [
            {
                "name": "ticker",
                "in": "query",
                "required": True,
                "schema": {"type": "string"},
            },
            {
                "name": "asset_class",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "enum": ["stocks", "options"], "default": "stocks"},
            },
            {
                "name": "feed",
                "in": "query",
                "required": False,
                "schema": {
                    "type": "string",
                    "enum": ["trades", "quotes", "aggregates_minute", "aggregates_second"],
                    "default": "trades",
                },
            },
            {
                "name": "max_events",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
        ],
        "responses": {
            "200": {"description": "Server-Sent Events stream from Massive"},
            "400": {"description": "Invalid stream request"},
            "501": {"description": "Massive streaming is not configured or enabled"},
        },
    },
    {
        "method": "GET",
        "path": "/api/openapi",
        "tag": "meta",
        "summary": "This document",
    },
    {
        "method": "GET",
        "path": "/api/mcp",
        "tag": "mcp",
        "summary": "MCP server descriptor",
    },
    {
        "method": "POST",
        "path": "/api/mcp",
        "tag": "mcp",
        "summary": "MCP streamable HTTP endpoint (JSON-RPC 2.0)",
    },
    {
        "method": "POST",
        "path": "/api/agent/chat",
        "tag": "agent",
        "summary": "Run one agent turn and return the final message",
        "request_schema": AGENT_REQUEST_SCHEMA,
    },
    {
        "method": "POST",
        "path": "/api/agent/chat/stream",
        "tag": "agent",
        "summary": "Run one agent turn as an NDJSON event stream",
        "request_schema": AGENT_REQUEST_SCHEMA,
        "success_media_type": "application/x-ndjson",
    },
    {
        "method": "POST",
        "path": "/api/tools/vision/stream",
        "tag": "research",
        "summary": "Classic memo NDJSON stream",
    },
    {
        "method": "POST",
        "path": "/api/tools/vision/v2/stream",
        "tag": "research",
        "summary": "Vision v2 NDJSON stream",
    },
    {
        "method": "POST",
        "path": "/api/tools/vision/v2/pdf",
        "tag": "research",
        "summary": "Vision v2 memo as a PDF download",
    },
    {
        "method": "POST",
        "path": "/api/tools/torque/scan/stream",
        "tag": "signals",
        "summary": "Torque scan NDJSON stream",
    },
    {
        "method": "GET",
        "path": "/api/alerts/scheduler/status",
        "tag": "alerts",
        "summary": "Daily alert scheduler configuration status",
    },
    {
        "method": "POST",
        "path": "/api/alerts/scheduled/run",
        "tag": "alerts",
        "summary": "Run saved daily alert rules (bearer token when configured)",
    },
    {
        "method": "POST",
        "path": "/api/alerts/webhook/test",
        "tag": "alerts",
        "summary": "Send a test webhook delivery for a saved rule",
    },
)


def build_openapi_document(base_url: str | None = None) -> dict[str, Any]:
    paths: dict[str, Any] = {}

    for spec in TOOLS:
        operation = _tool_operation(spec)
        paths.setdefault(spec.path, {})[spec.method.lower()] = operation

    for route in SUPPORTING_ROUTES:
        path = str(route["path"])
        method = str(route["method"]).lower()
        entry = paths.setdefault(path, {})
        if method in entry:
            continue
        responses = _responses()
        success_media_type = route.get("success_media_type")
        if isinstance(success_media_type, str):
            responses["200"]["content"] = {success_media_type: {"schema": {"type": "string"}}}
        supporting_operation: dict[str, Any] = {
            "operationId": _operation_id(str(route["method"]), path),
            "summary": route["summary"],
            "tags": [route["tag"]],
            "responses": responses,
        }
        parameters = route.get("parameters")
        if isinstance(parameters, list):
            supporting_operation["parameters"] = parameters
        success_schema = route.get("success_schema")
        if isinstance(success_schema, dict):
            responses["200"]["content"]["application/json"]["schema"] = success_schema
        error_responses = route.get("error_responses")
        if isinstance(error_responses, dict):
            for status, description in error_responses.items():
                responses[str(status)] = {
                    "description": str(description),
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Error"}}
                    },
                }
        request_schema = route.get("request_schema")
        if isinstance(request_schema, dict):
            supporting_operation["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": request_schema}},
            }
        entry[method] = supporting_operation

    document: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": API_TITLE,
            "version": API_VERSION,
            "description": API_DESCRIPTION,
        },
        "tags": [
            {"name": group, "description": description} for group, description in GROUPS.items()
        ]
        + [
            {"name": "mcp", "description": "Model Context Protocol transport"},
            {"name": "agent", "description": "The in-product research agent"},
            {"name": "alerts", "description": "Saved alert rules and delivery"},
        ],
        "paths": paths,
        "components": {
            "schemas": {
                "Error": {
                    "type": "object",
                    "properties": {"error": {"type": "string"}},
                    "required": ["error"],
                }
            }
        },
        "x-mcp": {
            "endpoint": "/api/mcp",
            "transport": "streamable-http",
            "tool_count": len(TOOLS),
            "authentication": "none",
        },
    }
    if base_url:
        document["servers"] = [{"url": base_url, "description": "This deployment"}]
    return document


def _tool_operation(spec: ToolSpec) -> dict[str, Any]:
    properties = spec.properties
    body_properties = {
        key: value for key, value in properties.items() if key not in spec.path_params
    }

    operation: dict[str, Any] = {
        "operationId": spec.name,
        "summary": spec.summary,
        "description": spec.describe(),
        "tags": [spec.group],
        "responses": _responses(spec.returns),
        "x-tool-name": spec.name,
        "x-cost": spec.cost,
    }

    parameters = [
        {
            "name": key,
            "in": "path",
            "required": True,
            "schema": {
                key2: value
                for key2, value in properties.get(key, {"type": "string"}).items()
                if key2 != "description"
            },
            "description": properties.get(key, {}).get("description", ""),
        }
        for key in spec.path_params
    ]

    if spec.method == "GET":
        parameters.extend(
            {
                "name": key,
                "in": "query",
                "required": key in spec.required,
                "schema": {k: v for k, v in value.items() if k != "description"},
                "description": value.get("description", ""),
            }
            for key, value in body_properties.items()
        )
    elif body_properties:
        required = [key for key in spec.required if key in body_properties]
        schema: dict[str, Any] = {
            "type": "object",
            "properties": body_properties,
            "additionalProperties": False,
        }
        if required:
            schema["required"] = required
        operation["requestBody"] = {
            "required": bool(required),
            "content": {"application/json": {"schema": schema}},
        }

    if parameters:
        operation["parameters"] = parameters
    return operation


def _responses(success: str = "Success") -> dict[str, Any]:
    return {
        "200": {
            "description": success or "Success",
            "content": {"application/json": {"schema": {"type": "object"}}},
        },
        "400": {
            "description": "Invalid request",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}},
        },
    }


def _operation_id(method: str, path: str) -> str:
    cleaned = path.strip("/").replace("/", ".").replace("{", "").replace("}", "")
    return f"{method.lower()}.{cleaned}"
