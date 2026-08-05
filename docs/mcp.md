# Underlying Analyzer MCP

stdio MCP server that calls the public Terminal HTTP API. No API key required.

Default base URL:

`https://underlying-terminal-production.up.railway.app`

Override with `UNDERLYING_BASE_URL` or `APP_URL`.

## Install

From this repo:

```bash
python -m pip install -e ".[mcp]"
```

Or rely on the main package deps (includes `mcp`).

## Run

```bash
UNDERLYING_BASE_URL=https://underlying-terminal-production.up.railway.app underlying-mcp
```

## Cursor config

Add to Cursor MCP settings (`~/.cursor/mcp.json` or project `.cursor/mcp.json`):

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

If the script is not on PATH, point command at the venv python module:

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

| Tool | Maps to |
| --- | --- |
| `health` | `GET /api/health` |
| `api_docs` | `GET /api/docs` |
| `analyze_ticker` | `GET /api/analysis/<ticker>` |
| `analyze_batch` | `POST /api/analysis` |
| `render_chart` | `POST /api/charts/<type>` |
| `stock_fax` | `POST /api/tools/fax` |
| `vision_memo` | `POST /api/tools/vision[/v2]` |
| `torque` | `POST /api/tools/torque` |
| `torque_scan` | `POST /api/tools/torque/scan` |
| `moneyline` | `POST /api/tools/moneyline` |
| `watchlist_cockpit` | `POST /api/watchlists/cockpit` |
| `resolve_watchlist` | `POST /api/watchlists/resolve` |
| `sec_source_pack` | `GET /api/sec/<ticker>` |
| `pixel_image` | `POST /api/tools/pixel` |

Image base64 payloads are omitted by default. Pass `include_images=true` on chart/image tools when you need them.

## HTTP docs

- Site: `/docs` and `/docs#api`
- Markdown: `/docs/api.md`
- Catalog JSON: `/api/docs`
