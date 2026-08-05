"""Canonical capability registry for The Underlying Analyzer.

This module is the single source of truth for every tool the product exposes.
Each :class:`ToolSpec` declares a name, human/agent-facing documentation, a JSON
Schema for its arguments, and the public HTTP route that implements it.

Everything else is derived from this registry:

* ``GET /api/docs`` - machine-readable catalog
* ``GET /api/openapi`` - OpenAPI 3.1 document
* ``POST /api/mcp`` - streamable HTTP MCP endpoint
* ``mcp_server.server`` - stdio MCP server
* ``app.agent`` - the in-product research agent's tool list

The registry deliberately holds no imports from the Flask app, so it can be
consumed by the stdio MCP server and by tests without booting the web app.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CHART_TYPES: tuple[str, ...] = (
    "auction",
    "performance",
    "regression",
    "ridge-growth",
    "flow-compass",
    "torque",
    "portfolio",
    "volatility",
)

GROUPS: dict[str, str] = {
    "meta": "Service metadata, capability discovery, and provider health",
    "charts": "Rendered chart packs for one ticker or a whole list",
    "research": "Narrative research: briefs, memos, filings, and news",
    "signals": "Quantitative scores and scans",
    "watchlists": "Watchlist resolution, ranking, and alert digests",
    "studio": "Generated imagery and publishable article artifacts",
}

# Cost hints let the agent (and the UI) reason about latency before calling.
COST_FAST = "fast"  # sub-second to a few seconds, pure data
COST_SLOW = "slow"  # multi-second, renders charts or scans many tickers
COST_LLM = "llm"  # calls a text model, tens of seconds


@dataclass(frozen=True)
class ToolSpec:
    """One capability, bound to exactly one public HTTP route."""

    name: str
    title: str
    group: str
    summary: str
    when_to_use: str
    method: str
    path: str
    input_schema: dict[str, Any]
    returns: str
    path_params: tuple[str, ...] = ()
    produces_images: bool = False
    cost: str = COST_FAST
    agent: bool = True
    mcp: bool = True
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def required(self) -> tuple[str, ...]:
        required = self.input_schema.get("required")
        return tuple(required) if isinstance(required, list) else ()

    @property
    def properties(self) -> dict[str, Any]:
        properties = self.input_schema.get("properties")
        return properties if isinstance(properties, dict) else {}

    def describe(self) -> str:
        """Full description string handed to MCP clients and the model."""
        parts = [self.summary.rstrip(".") + ".", self.when_to_use.strip()]
        if self.produces_images:
            parts.append(
                "Returns rendered chart images as artifacts; the tool result "
                "carries lightweight image refs instead of base64."
            )
        return " ".join(part for part in parts if part)


def _schema(
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


_TICKER = {"type": "string", "description": "Single ticker symbol, e.g. AAPL"}
_TICKERS = {
    "type": "string",
    "description": "Comma-separated tickers, e.g. 'AAPL, MSFT, NVDA'",
}
_WATCHLIST_URL = {
    "type": "string",
    "description": "Public TradingView watchlist URL to resolve into tickers",
}
_MAX_RESULTS = {
    "type": "integer",
    "minimum": 1,
    "maximum": 50,
    "default": 10,
    "description": "Cap on resolved tickers (1-50, default 10)",
}
_PERIOD = {
    "type": "string",
    "enum": ["1mo", "3mo", "6mo", "1y", "2y", "5y", "10y"],
    "default": "1y",
    "description": "History window",
}


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="list_capabilities",
        title="List capabilities",
        group="meta",
        summary="Return every tool, its arguments, when to use it, and its cost",
        when_to_use=(
            "Call first when you are unsure which tool answers a question, or "
            "when the user asks what this terminal can do."
        ),
        method="GET",
        path="/api/agent/tools",
        input_schema=_schema({}),
        returns="Capability catalog grouped by lane.",
    ),
    ToolSpec(
        name="health_check",
        title="Health check",
        group="meta",
        summary="Liveness probe for the terminal API",
        when_to_use="Use only to verify the service is reachable.",
        method="GET",
        path="/api/health",
        input_schema=_schema({}),
        returns="Service status object.",
    ),
    ToolSpec(
        name="provider_status",
        title="Provider status",
        group="meta",
        summary="Market data provider notes and fallback order",
        when_to_use=(
            "Use when a data call failed or the user asks where the numbers "
            "come from."
        ),
        method="GET",
        path="/api/providers",
        input_schema=_schema({}),
        returns="Primary/fallback provider names and caveats.",
    ),
    ToolSpec(
        name="analyze_ticker",
        title="Ticker brief",
        group="research",
        summary="Quote, fundamentals, scanner pass, and a written brief for one ticker",
        when_to_use=(
            "The default first call for 'what's going on with X'. Cheaper and "
            "faster than a full Vision memo."
        ),
        method="GET",
        path="/api/analysis/{ticker}",
        path_params=("ticker",),
        input_schema=_schema({"ticker": _TICKER}, required=["ticker"]),
        returns="Summary metrics plus a generated brief.",
        cost=COST_LLM,
    ),
    ToolSpec(
        name="analyze_batch",
        title="Batch brief",
        group="research",
        summary="Scanner pass and a comparative brief across several tickers",
        when_to_use=(
            "Use when the user names more than one ticker or points at a "
            "watchlist and wants a written comparison."
        ),
        method="POST",
        path="/api/analysis",
        input_schema=_schema(
            {
                "tickers": _TICKERS,
                "watchlist_url": _WATCHLIST_URL,
                "max_results": _MAX_RESULTS,
            }
        ),
        returns="Per-ticker summaries, scanner rows, and a comparative brief.",
        cost=COST_LLM,
    ),
    ToolSpec(
        name="render_chart",
        title="Render chart pack",
        group="charts",
        summary="Render one of eight chart packs for a ticker, list, or watchlist",
        when_to_use=(
            "Use whenever a visual would answer the question faster than prose: "
            "auction for value/acceptance, regression for trend health, "
            "performance for seasonality, volatility for regime, portfolio for "
            "a basket, ridge-growth / flow-compass / torque for signal state."
        ),
        method="POST",
        path="/api/charts/{chart_type}",
        path_params=("chart_type",),
        input_schema=_schema(
            {
                "chart_type": {
                    "type": "string",
                    "enum": list(CHART_TYPES),
                    "description": "Which chart pack to render",
                },
                "ticker": _TICKER,
                "tickers": _TICKERS,
                "watchlist_url": _WATCHLIST_URL,
                "max_results": _MAX_RESULTS,
                "period": _PERIOD,
                "month": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 12,
                    "description": "Month number for the performance / month-map pack",
                },
                "benchmark": {
                    "type": "string",
                    "description": "Benchmark ticker for portfolio runs (default SPY)",
                },
                "investment_per_stock": {
                    "type": "number",
                    "description": "Portfolio sizing per name",
                },
            },
            required=["chart_type"],
        ),
        returns="Chart images plus the metrics behind them.",
        produces_images=True,
        cost=COST_SLOW,
    ),
    ToolSpec(
        name="stock_fax",
        title="Stock Fax",
        group="research",
        summary="Narrative one-pager: business, numbers, and the current setup",
        when_to_use=(
            "Use for 'explain this company to me' questions where the user "
            "wants prose rather than levels."
        ),
        method="POST",
        path="/api/tools/fax",
        input_schema=_schema({"ticker": _TICKER}, required=["ticker"]),
        returns="Fax sections plus the underlying data pack.",
        cost=COST_LLM,
    ),
    ToolSpec(
        name="vision_memo",
        title="Vision memo",
        group="research",
        summary="Full analyst memo with SEC citations, news, and a rating",
        when_to_use=(
            "The deepest research call. Use for diligence, a written "
            "recommendation, or when the user asks for a memo or report. "
            "Slow - say what you are doing before calling it."
        ),
        method="POST",
        path="/api/tools/vision/v2",
        input_schema=_schema(
            {
                "ticker": _TICKER,
                "version": {
                    "type": "string",
                    "enum": ["v2", "classic"],
                    "default": "v2",
                    "description": "v2 adds citation verification and news context",
                },
            },
            required=["ticker"],
        ),
        returns="Memo text, parsed sections, citations, and source packs.",
        cost=COST_LLM,
    ),
    ToolSpec(
        name="sec_source_pack",
        title="SEC source pack",
        group="research",
        summary="EDGAR filing metadata, excerpts, and XBRL company facts",
        when_to_use=(
            "Use to ground a claim in primary sources, or when the user asks "
            "about filings, segments, or reported financials."
        ),
        method="GET",
        path="/api/sec/{ticker}",
        path_params=("ticker",),
        input_schema=_schema({"ticker": _TICKER}, required=["ticker"]),
        returns="Filing index, excerpts, and company facts.",
    ),
    ToolSpec(
        name="search_news",
        title="Search news",
        group="research",
        summary="Recent web and news results for a ticker or topic",
        when_to_use=(
            "Use for catalysts, 'why did it move', and anything time-sensitive. "
            "Always cite the URLs you use."
        ),
        method="POST",
        path="/api/news",
        input_schema=_schema(
            {
                "query": {
                    "type": "string",
                    "description": "Free-text topic or thesis to search",
                },
                "ticker": {
                    "type": "string",
                    "description": "Optional ticker to anchor the search",
                },
                "days_back": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 90,
                    "default": 14,
                    "description": "Only include results published within this window",
                },
                "num_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 12,
                    "default": 6,
                    "description": "How many articles to return",
                },
            }
        ),
        returns="Ranked articles with title, url, published date, and snippet.",
    ),
    ToolSpec(
        name="torque_score",
        title="Torque score",
        group="signals",
        summary="Misclassified-revenue-torque composite for one ticker",
        when_to_use=(
            "Use when the question is about inflection: revenue bend, margin "
            "torque, stale valuation, or operating leverage."
        ),
        method="POST",
        path="/api/tools/torque",
        input_schema=_schema({"ticker": _TICKER}, required=["ticker"]),
        returns="Torque score, stage, component breakdown, and a chart.",
        produces_images=True,
        cost=COST_SLOW,
    ),
    ToolSpec(
        name="torque_scan",
        title="Torque scan",
        group="signals",
        summary="Rank a list of tickers by torque stage and score",
        when_to_use=(
            "Use to find Coiled Spring / Inflecting / Proof Phase names across "
            "a watchlist."
        ),
        method="POST",
        path="/api/tools/torque/scan",
        input_schema=_schema(
            {
                "tickers": _TICKERS,
                "watchlist_url": _WATCHLIST_URL,
                "max_results": _MAX_RESULTS,
            }
        ),
        returns="One scored row per ticker, sorted by torque.",
        cost=COST_SLOW,
    ),
    ToolSpec(
        name="moneyline",
        title="Options moneyline",
        group="signals",
        summary="Options open-interest and gamma wall chart for an expiry",
        when_to_use=(
            "Use for options positioning questions: where are the walls, what "
            "strikes matter into expiry."
        ),
        method="POST",
        path="/api/tools/moneyline",
        input_schema=_schema(
            {
                "ticker": _TICKER,
                "expiry": {
                    "type": "string",
                    "description": "Expiry as YYYY-MM-DD; omit for the nearest",
                },
            },
            required=["ticker"],
        ),
        returns="Moneyline chart plus strike-level metrics.",
        produces_images=True,
        cost=COST_SLOW,
    ),
    ToolSpec(
        name="resolve_watchlist",
        title="Resolve watchlist",
        group="watchlists",
        summary="Turn a public TradingView watchlist URL into tickers",
        when_to_use=(
            "Call before any list-wide work when the user pastes a watchlist "
            "link and you need to know what is in it."
        ),
        method="POST",
        path="/api/watchlists/resolve",
        input_schema=_schema(
            {"watchlist_url": _WATCHLIST_URL, "max_results": _MAX_RESULTS},
            required=["watchlist_url"],
        ),
        returns="Watchlist metadata and the resolved ticker list.",
    ),
    ToolSpec(
        name="watchlist_cockpit",
        title="Watchlist cockpit",
        group="watchlists",
        summary="Rank a list by scanner strength, ridge state, flow, and location",
        when_to_use=(
            "The best 'where do I start' tool for a list. Use before drilling "
            "into individual names."
        ),
        method="POST",
        path="/api/watchlists/cockpit",
        input_schema=_schema(
            {
                "tickers": _TICKERS,
                "watchlist_url": _WATCHLIST_URL,
                "max_results": _MAX_RESULTS,
            }
        ),
        returns="Ranked cockpit rows with lane, score, and risk.",
        cost=COST_SLOW,
    ),
    ToolSpec(
        name="watchlist_alerts",
        title="Watchlist alerts",
        group="watchlists",
        summary="Prioritized digest of setups, risk flags, and regime changes",
        when_to_use=(
            "Use for 'what changed' and daily-review questions across a list."
        ),
        method="POST",
        path="/api/watchlists/alerts",
        input_schema=_schema(
            {
                "tickers": _TICKERS,
                "watchlist_url": _WATCHLIST_URL,
                "max_results": _MAX_RESULTS,
                "max_alerts": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 12,
                    "description": "Cap on returned alerts",
                },
                "volatility_threshold": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 2,
                    "default": 0.55,
                    "description": "Annualized vol level that triggers a flag",
                },
            }
        ),
        returns="Ordered alert digest with severity and reason.",
        cost=COST_SLOW,
    ),
    ToolSpec(
        name="compose_research_article",
        title="Compose research article",
        group="studio",
        summary="Publish a structured research brief with explicit recommendations",
        when_to_use=(
            "Call once, at the end, when the user asks for a write-up, memo, "
            "summary, or recommendations - or when you have gathered enough "
            "evidence that a saveable artifact is the right deliverable. Every "
            "claim should trace back to a tool you actually called."
        ),
        method="POST",
        path="/api/agent/article",
        input_schema=_schema(
            {
                "title": {"type": "string", "description": "Headline, under 90 chars"},
                "subtitle": {
                    "type": "string",
                    "description": "One-line framing of the question answered",
                },
                "thesis": {
                    "type": "string",
                    "description": "The single most important takeaway, 1-3 sentences",
                },
                "tickers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tickers this article covers",
                },
                "sections": {
                    "type": "array",
                    "description": "Body sections in reading order",
                    "items": {
                        "type": "object",
                        "properties": {
                            "heading": {"type": "string"},
                            "body": {
                                "type": "string",
                                "description": "Markdown paragraphs for this section",
                            },
                        },
                        "required": ["heading", "body"],
                    },
                },
                "recommendations": {
                    "type": "array",
                    "description": "Concrete, checkable actions with a stance",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ticker": {"type": "string"},
                            "stance": {
                                "type": "string",
                                "enum": [
                                    "constructive",
                                    "neutral",
                                    "cautious",
                                    "avoid",
                                    "watch",
                                ],
                            },
                            "action": {
                                "type": "string",
                                "description": "What to do, in one sentence",
                            },
                            "rationale": {"type": "string"},
                            "invalidation": {
                                "type": "string",
                                "description": "What would prove this wrong",
                            },
                            "confidence": {
                                "type": "string",
                                "enum": ["low", "medium", "high"],
                            },
                        },
                        "required": ["stance", "action"],
                    },
                },
                "risks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "What could break the thesis",
                },
                "sources": {
                    "type": "array",
                    "description": "Where the evidence came from",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "url": {"type": "string"},
                            "kind": {
                                "type": "string",
                                "enum": ["tool", "filing", "news", "chart", "other"],
                            },
                        },
                        "required": ["label"],
                    },
                },
            },
            required=["title", "thesis", "sections"],
        ),
        returns="A normalized article artifact plus rendered markdown.",
    ),
    ToolSpec(
        name="pixel_image",
        title="Pixel image",
        group="studio",
        summary="Generate an illustration from a text prompt",
        when_to_use=(
            "Use only when the user explicitly asks for artwork or a cover "
            "image. Never as a substitute for a chart."
        ),
        method="POST",
        path="/api/tools/pixel",
        input_schema=_schema(
            {"prompt": {"type": "string", "description": "Image prompt"}},
            required=["prompt"],
        ),
        returns="Generated image artifact.",
        produces_images=True,
        cost=COST_SLOW,
    ),
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOLS}


class ToolArgumentError(ValueError):
    """Raised when tool arguments do not satisfy the declared schema."""


def get_tool(name: str) -> ToolSpec:
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        known = ", ".join(sorted(TOOLS_BY_NAME))
        raise ToolArgumentError(f"Unknown tool '{name}'. Available tools: {known}")
    return spec


def agent_tools() -> tuple[ToolSpec, ...]:
    return tuple(spec for spec in TOOLS if spec.agent)


def mcp_tools() -> tuple[ToolSpec, ...]:
    return tuple(spec for spec in TOOLS if spec.mcp)


def anthropic_tool_definitions(
    specs: tuple[ToolSpec, ...] | None = None,
) -> list[dict[str, Any]]:
    """Tool blocks for the Anthropic Messages API."""
    return [
        {
            "name": spec.name,
            "description": spec.describe(),
            "input_schema": spec.input_schema,
        }
        for spec in (specs if specs is not None else agent_tools())
    ]


def mcp_tool_definitions() -> list[dict[str, Any]]:
    """Tool descriptors for the MCP ``tools/list`` response."""
    return [
        {
            "name": spec.name,
            "title": spec.title,
            "description": spec.describe(),
            "inputSchema": spec.input_schema,
            "annotations": {
                "title": spec.title,
                "readOnlyHint": True,
                "destructiveHint": False,
                "openWorldHint": True,
            },
        }
        for spec in mcp_tools()
    ]


def tool_catalog_payload() -> dict[str, Any]:
    """Compact capability catalog for the UI, ``/api/docs``, and the agent.

    Deliberately omits input schemas. The agent already carries them in its tool
    definitions, and repeating eighteen full schemas here would make
    ``list_capabilities`` one of the most expensive calls in the registry.
    Schemas live in ``GET /api/openapi`` and MCP ``tools/list``.
    """
    return {
        "ok": True,
        "tool_count": len(TOOLS),
        "groups": [
            {
                "id": group,
                "label": group.title(),
                "description": description,
                "tools": [spec.name for spec in TOOLS if spec.group == group],
            }
            for group, description in GROUPS.items()
        ],
        "tools": [
            {
                "name": spec.name,
                "title": spec.title,
                "group": spec.group,
                "summary": spec.summary,
                "when_to_use": spec.when_to_use,
                "returns": spec.returns,
                "cost": spec.cost,
                "produces_images": spec.produces_images,
                "agent": spec.agent,
                "mcp": spec.mcp,
                "http": {"method": spec.method, "path": spec.path},
                "arguments": sorted(spec.properties),
                "required": list(spec.required),
            }
            for spec in TOOLS
        ],
    }


def coerce_arguments(spec: ToolSpec, arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate required keys, drop unknown keys, and coerce simple scalars."""
    if not isinstance(arguments, dict):
        raise ToolArgumentError(f"{spec.name} arguments must be an object")

    properties = spec.properties
    cleaned: dict[str, Any] = {}
    for key, raw in arguments.items():
        if key not in properties:
            continue
        value = _coerce(properties[key], raw)
        if value is None:
            continue
        cleaned[key] = value

    missing = [key for key in spec.required if key not in cleaned]
    if missing:
        raise ToolArgumentError(
            f"{spec.name} is missing required argument(s): {', '.join(missing)}"
        )

    for key in spec.path_params:
        enum = properties.get(key, {}).get("enum")
        if enum and key in cleaned and cleaned[key] not in enum:
            allowed = ", ".join(str(item) for item in enum)
            raise ToolArgumentError(
                f"{spec.name}.{key} must be one of: {allowed} (got '{cleaned[key]}')"
            )
    return cleaned


def _coerce(schema: dict[str, Any], value: Any) -> Any:
    if value is None:
        return None
    kind = schema.get("type")
    try:
        if kind == "integer" and not isinstance(value, bool):
            return int(value)
        if kind == "number" and not isinstance(value, bool):
            return float(value)
        if kind == "boolean":
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)
        if kind == "string":
            text = str(value).strip()
            return text or None
        if kind == "array" and isinstance(value, str):
            parts = [part.strip() for part in value.split(",")]
            return [part for part in parts if part]
    except (TypeError, ValueError) as exc:
        raise ToolArgumentError(f"Could not read value {value!r}: {exc}") from exc
    return value


def build_request(
    spec: ToolSpec, arguments: dict[str, Any]
) -> tuple[str, str, dict[str, Any] | None, dict[str, Any]]:
    """Resolve a tool call into (method, path, json body, query params)."""
    cleaned = coerce_arguments(spec, arguments)

    path = spec.path
    remaining = dict(cleaned)
    for key in spec.path_params:
        value = remaining.pop(key, None)
        if value is None:
            raise ToolArgumentError(f"{spec.name} requires '{key}'")
        path = path.replace("{" + key + "}", str(value).strip())

    if spec.method == "GET":
        return spec.method, path, None, remaining
    return spec.method, path, remaining, {}
