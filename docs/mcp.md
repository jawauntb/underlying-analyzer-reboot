# Underlying Analyzer MCP

Two transports, one tool set. Every tool is generated from
[`app/tool_registry.py`](../app/tool_registry.py), the same declaration that
drives the HTTP API, the OpenAPI document, and the in-product agent at `/chat`.

No API key is required for either transport.

## Streamable HTTP (recommended)

`POST /api/mcp` speaks JSON-RPC 2.0. It is stateless — no session id, no
handshake beyond `initialize`. `GET /api/mcp` returns a descriptor of the server.

```json
{
  "mcpServers": {
    "underlying": {
      "url": "https://underlying-terminal-production.up.railway.app/api/mcp"
    }
  }
}
```

Try it directly:

```bash
curl -s -X POST https://underlying-terminal-production.up.railway.app/api/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools[].name'
```

Supported methods: `initialize`, `ping`, `tools/list`, `tools/call`,
`resources/list`, `resources/read`, `prompts/list`. Protocol versions
`2025-06-18`, `2025-03-26`, and `2024-11-05` are accepted.

Resources: `underlying://catalog/tools` and `underlying://catalog/openapi`.

## stdio

For clients that only speak stdio, or when you want the tools pointed at a local
Flask process.

```bash
python -m pip install -e ".[mcp]"
UNDERLYING_BASE_URL=https://underlying-terminal-production.up.railway.app underlying-mcp
```

Default base URL is the Railway production deployment; override with
`UNDERLYING_BASE_URL` or `APP_URL`.

```json
{
  "mcpServers": {
    "underlying-analyzer": {
      "command": "underlying-mcp",
      "env": {
        "UNDERLYING_BASE_URL": "https://underlying-terminal-production.up.railway.app"
      }
    }
  }
}
```

If the script is not on PATH:

```json
{
  "mcpServers": {
    "underlying-analyzer": {
      "command": "python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/absolute/path/to/underlying-analyzer-reboot",
      "env": {
        "UNDERLYING_BASE_URL": "https://underlying-terminal-production.up.railway.app"
      }
    }
  }
}
```

## Tools

The catalog is generated, so the authoritative list is always live:

- `GET /api/agent/tools` — full catalog with schemas, cost hints, and routing guidance
- `GET /api/openapi` — the same surface as OpenAPI 3.1
- `/docs#mcp` — rendered in the browser

| Group | Tools |
| --- | --- |
| meta | `list_capabilities`, `health_check`, `provider_status` |
| research | `analyze_ticker`, `analyze_batch`, `stock_fax`, `vision_memo`, `sec_source_pack`, `search_news` |
| charts | `render_chart` |
| data | `chart_data`, `torque_data`, `moneyline_data` |
| signals | `torque_score`, `torque_scan`, `moneyline` |
| watchlists | `resolve_watchlist`, `watchlist_cockpit`, `watchlist_alerts` |
| studio | `compose_research_article`, `pixel_image` |

Start with `list_capabilities` if you are unsure which tool fits a question — it
returns when-to-use guidance and a cost hint (`fast`, `slow`, `llm`) for each.

## Images

Chart tools render real PNGs. Both transports omit the base64 payload by default
and hand back a short reference instead, so a tool result stays small. Pass
`include_images: true` when you actually need the bytes:

- **stdio** — adds the base64 back into `body`
- **streamable HTTP** — appends MCP `image` content blocks to the result

## Safety

Read-only research tooling. There is no broker integration and no order
execution path anywhere in the registry.

## HTTP docs

- Site: `/docs`, `/docs#mcp`, `/docs#api`
- Markdown: `/docs/api.md`
- Catalog JSON: `/api/docs`
- OpenAPI: `/api/openapi`
