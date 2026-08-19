"""Public machine-readable catalog of HTTP API routes and tools.

The route list below documents the whole HTTP surface. The agent/MCP tool
surface is derived from :mod:`app.tool_registry` so the catalog can never
disagree with what the tools actually do.
"""

from __future__ import annotations

from typing import Any

from app.mcp_http import PROTOCOL_VERSION, SERVER_VERSION
from app.tool_registry import TOOLS, tool_catalog_payload

PUBLIC_BASE_NOTE = (
    "All listed endpoints are publicly callable without an API key. "
    "Alert scheduler and webhook-test routes optionally use bearer tokens when configured."
)

API_ENDPOINTS: list[dict[str, Any]] = [
    {
        "method": "GET",
        "path": "/api/health",
        "group": "meta",
        "summary": "Liveness check",
        "auth": "none",
    },
    {
        "method": "GET",
        "path": "/api/docs",
        "group": "meta",
        "summary": "Machine-readable API catalog (this document)",
        "auth": "none",
    },
    {
        "method": "GET",
        "path": "/api/config",
        "group": "meta",
        "summary": "Public Supabase client config",
        "auth": "none",
    },
    {
        "method": "GET",
        "path": "/api/providers",
        "group": "meta",
        "summary": "Market data provider notes",
        "auth": "none",
    },
    {
        "method": "GET",
        "path": "/api/openapi",
        "group": "meta",
        "summary": "OpenAPI 3.1 document for the whole HTTP surface",
        "auth": "none",
    },
    {
        "method": "GET",
        "path": "/api/mcp",
        "group": "mcp",
        "summary": "MCP server descriptor",
        "auth": "none",
    },
    {
        "method": "POST",
        "path": "/api/mcp",
        "group": "mcp",
        "summary": "MCP streamable HTTP endpoint (JSON-RPC 2.0)",
        "auth": "none",
    },
    {
        "method": "GET",
        "path": "/api/agent/tools",
        "group": "agent",
        "summary": "Capability catalog the agent routes against",
        "auth": "none",
    },
    {
        "method": "POST",
        "path": "/api/agent/chat",
        "group": "agent",
        "summary": "Run one agent turn and return the final message",
        "auth": "none",
        "body": {
            "messages": "[{role, content}]",
            "tools": "string[]?",
            "tool_policy": "'exact'?",
            "context": "string?",
        },
    },
    {
        "method": "POST",
        "path": "/api/agent/chat/stream",
        "group": "agent",
        "summary": "Run one agent turn as an NDJSON event stream",
        "auth": "none",
        "body": {
            "messages": "[{role, content}]",
            "tools": "string[]?",
            "tool_policy": "'exact'?",
            "context": "string?",
        },
    },
    {
        "method": "POST",
        "path": "/api/agent/article",
        "group": "agent",
        "summary": "Normalize and render a research article artifact",
        "auth": "none",
    },
    {
        "method": "POST",
        "path": "/api/news",
        "group": "research",
        "summary": "Recent news and web results for a ticker or topic",
        "auth": "none",
        "body": {
            "query": "string",
            "ticker": "string?",
            "days_back": "int (default 14)",
            "num_results": "int (default 6)",
        },
    },
    {
        "method": "GET",
        "path": "/docs",
        "group": "meta",
        "summary": "Human docs page",
        "auth": "none",
    },
    {
        "method": "GET",
        "path": "/docs/api.md",
        "group": "meta",
        "summary": "Raw API markdown",
        "auth": "none",
    },
    {
        "method": "GET",
        "path": "/docs/chart-data-rendering.md",
        "group": "meta",
        "summary": "Chart-data rendering guide for upstream UIs (schemas + style)",
        "auth": "none",
    },
    {
        "method": "POST",
        "path": "/api/charts/{chart_type}",
        "group": "charts",
        "summary": "Render chart pack",
        "auth": "none",
        "path_params": {
            "chart_type": [
                "auction",
                "performance",
                "regression",
                "ridge-growth",
                "flow-compass",
                "torque",
                "portfolio",
                "volatility",
            ]
        },
        "body": {
            "ticker": "string",
            "tickers": "string | string[]",
            "watchlist_url": "string",
            "max_results": "int (default 10, max 50)",
            "period": "string",
            "month": "int",
            "start_date": "YYYY-MM-DD",
            "end_date": "YYYY-MM-DD",
            "investment_per_stock": "number",
            "benchmark": "string",
        },
    },
    {
        "method": "POST",
        "path": "/api/data/charts/{chart_type}",
        "group": "data",
        "summary": "Chartable JSON series (same packs as /api/charts, no images)",
        "auth": "none",
        "path_params": {
            "chart_type": [
                "auction",
                "performance",
                "regression",
                "ridge-growth",
                "flow-compass",
                "torque",
                "portfolio",
                "volatility",
            ]
        },
        "body": {
            "ticker": "string",
            "tickers": "string | string[]",
            "watchlist_url": "string",
            "max_results": "int (default 10, max 50)",
            "period": "string",
            "month": "int",
            "start_date": "YYYY-MM-DD",
            "end_date": "YYYY-MM-DD",
            "investment_per_stock": "number",
            "benchmark": "string",
        },
    },
    {
        "method": "GET",
        "path": "/api/analysis/{ticker}",
        "group": "analysis",
        "summary": "Single-ticker summary + brief",
        "auth": "none",
    },
    {
        "method": "POST",
        "path": "/api/analysis",
        "group": "analysis",
        "summary": "Batch analysis for tickers or watchlist",
        "auth": "none",
    },
    {
        "method": "POST",
        "path": "/api/watchlists/resolve",
        "group": "watchlists",
        "summary": "Resolve public TradingView watchlist",
        "auth": "none",
    },
    {
        "method": "POST",
        "path": "/api/watchlists/cockpit",
        "group": "watchlists",
        "summary": "Ranked cockpit table",
        "auth": "none",
    },
    {
        "method": "POST",
        "path": "/api/watchlists/alerts",
        "group": "watchlists",
        "summary": "Watchlist alert digest",
        "auth": "none",
    },
    {
        "method": "GET",
        "path": "/api/alerts/scheduler/status",
        "group": "alerts",
        "summary": "Scheduler configuration status",
        "auth": "none",
    },
    {
        "method": "POST",
        "path": "/api/alerts/scheduled/run",
        "group": "alerts",
        "summary": "Run saved daily alert rules",
        "auth": "bearer ALERT_SCHEDULER_TOKEN (when configured)",
    },
    {
        "method": "POST",
        "path": "/api/alerts/webhook/test",
        "group": "alerts",
        "summary": "Test webhook delivery for a saved rule",
        "auth": "bearer Supabase user access token",
    },
    {
        "method": "GET",
        "path": "/api/sec/{ticker}",
        "group": "sec",
        "summary": "SEC EDGAR source pack",
        "auth": "none",
    },
    {
        "method": "POST",
        "path": "/api/tools/fax",
        "group": "tools",
        "summary": "Stock Fax narrative pack",
        "auth": "none",
        "body": {"ticker": "string"},
    },
    {
        "method": "POST",
        "path": "/api/tools/vision",
        "group": "tools",
        "summary": "Classic Market Memo",
        "auth": "none",
        "body": {"ticker": "string"},
    },
    {
        "method": "POST",
        "path": "/api/tools/vision/stream",
        "group": "tools",
        "summary": "Classic memo NDJSON stream",
        "auth": "none",
    },
    {
        "method": "POST",
        "path": "/api/tools/vision/v2",
        "group": "tools",
        "summary": "Vision v2 memo",
        "auth": "none",
        "body": {"ticker": "string"},
    },
    {
        "method": "POST",
        "path": "/api/tools/vision/v2/stream",
        "group": "tools",
        "summary": "Vision v2 NDJSON stream",
        "auth": "none",
    },
    {
        "method": "POST",
        "path": "/api/tools/vision/v2/pdf",
        "group": "tools",
        "summary": "Vision v2 PDF download",
        "auth": "none",
    },
    {
        "method": "POST",
        "path": "/api/tools/torque",
        "group": "tools",
        "summary": "Torque score + chart",
        "auth": "none",
        "body": {"ticker": "string"},
    },
    {
        "method": "POST",
        "path": "/api/data/tools/torque",
        "group": "data",
        "summary": "Torque score + chartable series (no image)",
        "auth": "none",
        "body": {"ticker": "string"},
    },
    {
        "method": "POST",
        "path": "/api/tools/torque/scan",
        "group": "tools",
        "summary": "Batch torque scan",
        "auth": "none",
    },
    {
        "method": "POST",
        "path": "/api/tools/torque/scan/stream",
        "group": "tools",
        "summary": "Torque scan NDJSON stream",
        "auth": "none",
    },
    {
        "method": "POST",
        "path": "/api/tools/moneyline",
        "group": "tools",
        "summary": "Options moneyline chart",
        "auth": "none",
        "body": {"ticker": "string", "expiry": "YYYY-MM-DD?"},
    },
    {
        "method": "POST",
        "path": "/api/data/tools/moneyline",
        "group": "data",
        "summary": "Options moneyline ladder data (no image)",
        "auth": "none",
        "body": {"ticker": "string", "expiry": "YYYY-MM-DD?"},
    },
    {
        "method": "POST",
        "path": "/api/tools/pixel",
        "group": "tools",
        "summary": "Pixel image generation",
        "auth": "none",
        "body": {"prompt": "string"},
    },
]

SITE_TOOLS: list[dict[str, str]] = [
    {"id": "terminal", "path": "/", "summary": "Chart modes, cockpit, briefs, alerts"},
    {
        "id": "chat",
        "path": "/chat",
        "summary": "Agent chat over every tool, with saved briefs and history",
    },
    {"id": "vision", "path": "/vision", "summary": "Market memo with SEC citations"},
    {"id": "pixel", "path": "/pixel", "summary": "Prompt to image"},
    {"id": "fax", "path": "/fax", "summary": "Stock Fax narrative pack"},
    {"id": "moneyline", "path": "/moneyline", "summary": "Options moneyline chart"},
    {"id": "docs", "path": "/docs", "summary": "Product + API documentation"},
    {"id": "design", "path": "/design", "summary": "Design system sandbox"},
]


def build_api_docs_payload(*, base_url: str | None = None) -> dict[str, Any]:
    catalog = tool_catalog_payload()
    return {
        "ok": True,
        "service": "underlying-analyzer-reboot",
        "version": SERVER_VERSION,
        "public": True,
        "note": PUBLIC_BASE_NOTE,
        "base_url": base_url,
        "docs": {
            "html": "/docs",
            "api_html": "/docs#api",
            "markdown": "/docs/api.md",
            "catalog": "/api/docs",
            "openapi": "/api/openapi",
        },
        "mcp": {
            "endpoint": "/api/mcp",
            "transport": "streamable-http",
            "protocol_version": PROTOCOL_VERSION,
            "authentication": "none",
            "stdio_command": "underlying-mcp",
            "tool_count": len(TOOLS),
        },
        "agent": {
            "chat": "/chat",
            "stream": "/api/agent/chat/stream",
            "tools": "/api/agent/tools",
        },
        "site_tools": SITE_TOOLS,
        "endpoints": API_ENDPOINTS,
        "tool_groups": catalog["groups"],
        "tools": catalog["tools"],
    }
