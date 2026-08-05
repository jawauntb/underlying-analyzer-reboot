"""stdio MCP server for The Underlying Analyzer public HTTP API.

Tools are not hand-written here. They are generated from
:mod:`app.tool_registry`, the same declaration that drives the HTTP API, the
``/api/mcp`` streamable endpoint, and the in-product agent. Adding a tool to the
registry adds it to every surface at once.

This process talks to a deployment over HTTPS, so it works against production
without running the Flask app locally.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from urllib.parse import urlencode

import requests

from app.tool_registry import (
    CHART_TYPES,
    ToolArgumentError,
    build_request,
    get_tool,
    mcp_tool_definitions,
    tool_catalog_payload,
)

__all__ = [
    "CHART_TYPES",
    "call_tool",
    "list_tools",
    "main",
]

DEFAULT_BASE_URL = "https://underlying-terminal-production.up.railway.app"
MAX_TEXT_CHARS = 200

INSTRUCTIONS = (
    "Chart-led market research tools from The Underlying Analyzer Terminal. "
    "No API key is required. Call list_capabilities first if you are unsure "
    "which tool fits a question. Rendered chart images are omitted by default; "
    "pass include_images=true when you actually need the base64 payload."
)


def _base_url() -> str:
    return (
        os.getenv("UNDERLYING_BASE_URL") or os.getenv("APP_URL") or DEFAULT_BASE_URL
    ).rstrip("/")


def _timeout() -> float:
    raw = os.getenv("UNDERLYING_MCP_TIMEOUT", "180")
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 180.0


def _strip_heavy_fields(value: Any) -> Any:
    """Replace base64 blobs with a short placeholder so results stay readable."""
    if isinstance(value, list):
        return [_strip_heavy_fields(item) for item in value]
    if not isinstance(value, dict):
        return value

    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        is_blob = key in {"data", "image", "b64_json"} and isinstance(item, str)
        if is_blob and len(item) > MAX_TEXT_CHARS:
            cleaned[key] = f"<omitted base64 ({len(item)} chars)>"
            continue
        cleaned[key] = _strip_heavy_fields(item)
    return cleaned


def list_tools() -> list[dict[str, Any]]:
    """Tool descriptors, with the transport-level include_images switch added."""
    tools = mcp_tool_definitions()
    for tool in tools:
        schema = json.loads(json.dumps(tool["inputSchema"]))
        properties = schema.setdefault("properties", {})
        properties["include_images"] = {
            "type": "boolean",
            "default": False,
            "description": "Return base64 image payloads instead of placeholders",
        }
        tool["inputSchema"] = schema
    return tools


def call_tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one registry tool against the configured deployment."""
    args = dict(arguments or {})
    include_images = bool(args.pop("include_images", False))

    try:
        spec = get_tool(name)
        method, path, body, query = build_request(spec, args)
    except ToolArgumentError as exc:
        return {"ok": False, "error": str(exc)}

    url = f"{_base_url()}{path}" + (f"?{urlencode(query)}" if query else "")
    try:
        response = requests.request(
            method,
            url,
            json=body if method != "GET" else None,
            timeout=_timeout(),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
    except requests.RequestException as exc:
        return {"ok": False, "error": f"{spec.title} request failed: {exc}", "url": url}

    try:
        payload: Any = response.json()
    except ValueError:
        payload = {"raw": response.text[:4000]}
    if not isinstance(payload, dict):
        payload = {"data": payload}

    return {
        "ok": response.ok,
        "status_code": response.status_code,
        "url": url,
        "body": payload if include_images else _strip_heavy_fields(payload),
    }


async def _serve() -> None:
    import mcp.types as types
    from mcp.server.lowlevel import NotificationOptions, Server
    from mcp.server.models import InitializationOptions
    from mcp.server.stdio import stdio_server

    server: Server[Any, Any] = Server("underlying-analyzer", instructions=INSTRUCTIONS)

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=tool["name"],
                title=tool.get("title"),
                description=tool["description"],
                inputSchema=tool["inputSchema"],
            )
            for tool in list_tools()
        ]

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        result = await asyncio.to_thread(call_tool, name, arguments)
        return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    @server.list_resources()
    async def handle_list_resources() -> list[types.Resource]:
        return [
            types.Resource(
                uri=types.AnyUrl("underlying://catalog/tools"),
                name="Tool catalog",
                description="Every tool with arguments, routing guidance, and cost.",
                mimeType="application/json",
            )
        ]

    @server.read_resource()
    async def handle_read_resource(uri: types.AnyUrl) -> str:
        if str(uri) != "underlying://catalog/tools":
            raise ValueError(f"Unknown resource '{uri}'")
        return json.dumps(tool_catalog_payload(), indent=2)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="underlying-analyzer",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
                instructions=INSTRUCTIONS,
            ),
        )


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
