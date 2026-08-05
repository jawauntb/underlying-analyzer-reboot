"""Streamable HTTP MCP endpoint.

Implements the JSON-RPC 2.0 surface of the Model Context Protocol directly over
Flask, so any MCP client can point at ``POST /api/mcp`` with no extra process.
The server is stateless: no session id is required, each request is answered on
its own, and every tool is served from :mod:`app.tool_registry`.
"""

from __future__ import annotations

import json
from typing import Any

from app.tool_executor import execute_tool
from app.tool_registry import (
    ToolArgumentError,
    coerce_arguments,
    get_tool,
    mcp_tool_definitions,
    tool_catalog_payload,
)

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "underlying-analyzer"
SERVER_VERSION = "1.0.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = (
    "Chart-led market research tools from The Underlying Analyzer Terminal. "
    "No API key is required. Call list_capabilities first if you are unsure "
    "which tool fits. Chart tools return rendered images; pass "
    "include_images=false semantics by ignoring the artifact payload. This "
    "server is read-only research tooling and has no order execution path."
)

RESOURCES: tuple[dict[str, str], ...] = (
    {
        "uri": "underlying://catalog/tools",
        "name": "Tool catalog",
        "title": "Tool catalog",
        "description": "Every tool with arguments, routing guidance, and cost.",
        "mimeType": "application/json",
    },
    {
        "uri": "underlying://catalog/openapi",
        "name": "OpenAPI document",
        "title": "OpenAPI document",
        "description": "OpenAPI 3.1 description of the public HTTP API.",
        "mimeType": "application/json",
    },
)


def server_descriptor(base_url: str | None = None) -> dict[str, Any]:
    """Human/agent-readable description served from ``GET /api/mcp``."""
    return {
        "ok": True,
        "name": SERVER_NAME,
        "version": SERVER_VERSION,
        "transport": "streamable-http",
        "protocol_version": PROTOCOL_VERSION,
        "endpoint": "/api/mcp",
        "base_url": base_url,
        "authentication": "none",
        "tool_count": len(mcp_tool_definitions()),
        "instructions": INSTRUCTIONS,
        "methods": [
            "initialize",
            "ping",
            "tools/list",
            "tools/call",
            "resources/list",
            "resources/read",
            "prompts/list",
        ],
        "hint": (
            "POST JSON-RPC 2.0 to this URL. GET returns this descriptor. "
            "Example: {\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}"
        ),
    }


def handle_mcp_payload(payload: Any) -> Any:
    """Dispatch a JSON-RPC request, notification, or batch.

    Returns the response object (or list for batches), or ``None`` when the
    payload contained only notifications and nothing should be sent back.
    """
    if isinstance(payload, list):
        if not payload:
            return _error(None, INVALID_REQUEST, "Batch must not be empty")
        responses = [
            response
            for response in (handle_mcp_payload(item) for item in payload)
            if response is not None
        ]
        return responses or None

    if not isinstance(payload, dict):
        return _error(None, INVALID_REQUEST, "Request must be a JSON object")

    request_id = payload.get("id")
    method = payload.get("method")
    if not isinstance(method, str):
        return _error(request_id, INVALID_REQUEST, "Missing method")

    params = payload.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _error(request_id, INVALID_PARAMS, "params must be an object")

    is_notification = "id" not in payload

    try:
        result = _dispatch(method, params)
    except ToolArgumentError as exc:
        return None if is_notification else _error(request_id, INVALID_PARAMS, str(exc))
    except _MethodNotFound as exc:
        return None if is_notification else _error(request_id, METHOD_NOT_FOUND, str(exc))
    except Exception as exc:  # pragma: no cover - defensive boundary
        return (
            None
            if is_notification
            else _error(request_id, INTERNAL_ERROR, f"Internal error: {exc}")
        )

    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


class _MethodNotFound(Exception):
    pass


def _dispatch(method: str, params: dict[str, Any]) -> Any:
    if method == "initialize":
        return _initialize(params)
    if method == "ping":
        return {}
    if method.startswith("notifications/"):
        return {}
    if method == "tools/list":
        return {"tools": mcp_tool_definitions()}
    if method == "tools/call":
        return _tools_call(params)
    if method == "resources/list":
        return {"resources": list(RESOURCES)}
    if method == "resources/read":
        return _resources_read(params)
    if method == "prompts/list":
        return {"prompts": []}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    raise _MethodNotFound(f"Unknown method '{method}'")


def _initialize(params: dict[str, Any]) -> dict[str, Any]:
    requested = str(params.get("protocolVersion") or PROTOCOL_VERSION)
    negotiated = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
    return {
        "protocolVersion": negotiated,
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {"listChanged": False, "subscribe": False},
        },
        "serverInfo": {
            "name": SERVER_NAME,
            "title": "The Underlying Analyzer",
            "version": SERVER_VERSION,
        },
        "instructions": INSTRUCTIONS,
    }


def _tools_call(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ToolArgumentError("tools/call requires a tool name")

    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise ToolArgumentError("tools/call arguments must be an object")

    spec = get_tool(name.strip())
    include_images = bool(arguments.pop("include_images", False))

    # MCP separates protocol errors from tool errors: an unknown tool or bad
    # arguments is -32602, while a tool that ran and failed comes back as a
    # result with isError set. Validate first so each lands in the right place.
    coerce_arguments(spec, arguments)

    result = execute_tool(spec.name, arguments, keep_images=include_images)

    content: list[dict[str, Any]] = []
    if result.ok:
        content.append({"type": "text", "text": result.model_text()})
        if include_images:
            for artifact in result.artifacts:
                if not artifact.data:
                    continue
                content.append(
                    {
                        "type": "image",
                        "data": artifact.data,
                        "mimeType": artifact.mime,
                    }
                )
    else:
        content.append(
            {"type": "text", "text": result.error or f"{spec.title} failed"}
        )

    return {
        "content": content,
        "isError": not result.ok,
        "structuredContent": {"result": result.result} if result.ok else None,
    }


def _resources_read(params: dict[str, Any]) -> dict[str, Any]:
    uri = str(params.get("uri") or "")
    if uri == "underlying://catalog/tools":
        text = json.dumps(tool_catalog_payload(), indent=2)
    elif uri == "underlying://catalog/openapi":
        from app.openapi import build_openapi_document

        text = json.dumps(build_openapi_document(), indent=2)
    else:
        raise ToolArgumentError(f"Unknown resource '{uri}'")
    return {
        "contents": [
            {"uri": uri, "mimeType": "application/json", "text": text}
        ]
    }


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def parse_error_response() -> dict[str, Any]:
    return _error(None, PARSE_ERROR, "Request body is not valid JSON")
