# Underlying Analyzer MCP

Three ways in. The first two serve the whole tool registry (30 tools), which is generated from
[`app/tool_registry.py`](../app/tool_registry.py), the same declaration that drives the HTTP API,
the OpenAPI document, and the in-product agent at `/chat`. The third is a separate, smaller
allowlist for the constellation (see [Constellation MCP](#constellation-mcp-post-mcp)).

| Way in | What it serves | For |
| --- | --- | --- |
| `POST /api/mcp` | the whole registry, 30 tools | MCP clients you run yourself |
| `underlying-mcp` (stdio) | the whole registry | clients that only speak stdio |
| `POST /mcp` | 10 read-only tools and `ask_lattice_animals` | the lattice animals and the other constellation sites |

No API key is required for any of them.

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

The registry has 30 tools (`tests/test_constellation_docs.py` checks that this page names every
one):

| Group | Tools |
| --- | --- |
| meta | `list_capabilities`, `health_check`, `provider_status` |
| research | `analyze_ticker`, `analyze_batch`, `stock_fax`, `vision_memo`, `sec_source_pack`, `search_news`, `situate`, `situate_get`, `situate_chat`, `situate_export`, `prism_memo`, `prism_get`, `prism_chat`, `prism_export` |
| charts | `render_chart` |
| data | `chart_data`, `ticker_research_bundle`, `torque_data`, `moneyline_data` |
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

## Constellation MCP: `POST /mcp`

The Underlying Analyzer is one of six sites that each serve a small MCP and can call the others'
(the contract is `docs/constellation.md` in the
[lattice-animal](https://github.com/jawauntb/lattice-animal) repo). Its member name is
`underlying-analyzer`. This is its MCP: a short, explicit allowlist of read-only, cheap tools that
the lattice animals can call as tools, and a tool for asking them back.

| Route | What it is |
| --- | --- |
| `POST /mcp` | This site's tools. Stateless JSON-RPC 2.0 over streamable HTTP: `initialize`, `ping`, `tools/list`, `tools/call`. A notification gets `202`, a batch gets `400`, and `GET` or `DELETE` get `405` with `Allow: POST`. Registered exactly, because the lattice client refuses redirects. |
| `POST /mcp/lattice` | The lattice animals' own MCP, relayed one hop deeper. A name that is not in this site's peer registry is `404`. |
| `GET /.well-known/mcp.json` | The manifest: endpoint, tools with `readOnly`, peers, and the hop limit. Also at `/.well-known/mcp/server-card.json`. |

The protocol, the relay, the manifest and the hop rule are `app/mcp_lite.py`, lattice-animal's
library, copied verbatim. `tests/test_mcp_lite_vectors.py` runs the shared conformance vectors
against it and compares both files with the digests of the copies they came from.

### Why it is not `/api/mcp`

Two things about `/api/mcp` (in `app/mcp_http.py` and `app/tool_registry.py`), which is left as it
was:

- `ToolSpec.mcp = False` hides a tool from `tools/list`, but `tools/call` looks a name up with
  `get_tool` and does not read the flag, so a hidden tool can still be called by name. No
  registry tool sets `mcp=False` today.
- `tools/list` puts `readOnlyHint: true` on every tool, including `analyze_ticker`,
  `vision_memo`, `situate` and `prism_memo`, which call a language model or fan out to paid
  providers.

So `/mcp` has its own allowlist: a tuple of names in `app/constellation_mcp.py`. `ToolSpec.mcp`
is not read there. The tests flip every registry `mcp` flag both ways and show that what `/mcp`
offers does not move, and that each of the 21 registry tools off the list answers `-32602`
(unknown tool).

### The tools

Every tool reads: none trades, sends, saves or changes anything a user can see (`peer_forecast`
can fill a cache, below), and none takes a URL, watchlist, user id or key. The registry tools run
in process through `execute_tool`; the reads with no registry entry go through their own routes. A
result is compact JSON under the 8000 characters the library keeps: series are cut to their point
count, date range and last 5 points, and floats are rounded to 6 significant digits. A result
that is still too big has its strings cut with `…` and its lists ended with `… N more`, and one
that cannot be brought under the limit is a `{"truncated": true, ...}` envelope, which is valid
JSON; the tests check the size and the JSON for the packs and the tools, not for every payload a
provider could send.

| Tool | Arguments | What it returns | Route it reaches |
| --- | --- | --- | --- |
| `list_capabilities` | none | The terminal's tools with lane and cost class, and which of them this server runs | none (read from the registry) |
| `health_check` | none | `{ok, service}` | `GET /api/health` |
| `provider_status` | none | Primary and fallback market data provider, whether Massive is configured (not the key), streaming status | `GET /api/providers` |
| `sec_source_pack` | `ticker` | EDGAR filing metadata, excerpts cut to about 300 characters, key XBRL facts | `GET /api/sec/{ticker}` |
| `chart_data` | `chart_type` (`auction`, `performance`, `regression`, `ridge-growth`, `flow-compass`, `volatility`), `ticker`, optional `period`, `interval`, `month` | Levels and metadata in full, each series summarized. `ridge-growth` returns each window's results and the 1y window's series | `POST /api/data/charts/{chart_type}` |
| `torque_data` | `ticker` | Torque score, stage, recommendation, components, summarized price and fundamental series | `POST /api/data/tools/torque` |
| `moneyline_data` | `ticker`, optional `expiry` (`YYYY-MM-DD`) | Open-interest ladder for the strikes nearest spot | `POST /api/data/tools/moneyline` |
| `situate_get` | `ticker`, optional `as_of` | The stored Situate summary. An error when nothing is stored | `GET /api/situate/{ticker}/summary` |
| `prism_get` | `ticker`, optional `as_of` | The stored Prism summary. An error when nothing is stored | `GET /api/prism/{ticker}/summary` |
| `peer_forecast` | `ticker`, optional `horizon` (1, 2, 3, 6 or 12 months) | The TabICL v2 sector peer forecast: bucket, probabilities, expected excess return, confidence and the peers' ranks. A `503` reason comes through as `unavailable: <reason>` | `GET /api/tabular/peer-forecast/{ticker}` |
| `ask_lattice_animals` | `question` (1 to 600 characters; longer is cut), optional `to` (`field`, `app` or `connectome`) | The lattice animals' answer, relayed one hop deeper. One model call on their side, so slow | `POST` to the hub's `/mcp` |

Notes on the ones with a catch:

- `chart_data` covers the six single-ticker packs. `torque` is `torque_data`, `peer-forecast` is
  `peer_forecast`, and `portfolio` needs several tickers and a benchmark, so it is not offered.
- `situate_get` and `prism_get` read the `/summary` routes, the bounded projection, not the full
  packet. The tests replace the Situate and Prism build functions with ones that fail the test if
  called, and a ticker with nothing stored is still an error result.
- `peer_forecast` takes no `as_of`. The route hands `as_of` to its cache key without validating
  it, so each new date a caller sent would buy a fresh sector build (a panel load and a model
  call). Without it the result is cached per sector, horizon and day for 12 hours, per process.
  A cold sector loads a price panel from the market data provider and fills the Prism series cache
  (on disk, and in Supabase when it is configured), and with a model configured
  (`TABULAR_INFERENCE_URL`) it calls the model once for the live cross-section and once for the
  backtest. When the model is not configured, the ticker is outside the curated sector universe,
  or the model is down, the tool answers with the reason the route gave.
- A cold `peer_forecast` or `sec_source_pack` can take longer than the hub's 25 second deadline
  for an outbound call. The work finishes on this side and is cached (12 hours for the forecast,
  6 hours for the SEC pack), so a second call is quick.
- `provider_status` answers from `GET /api/providers`. The test sets a `MASSIVE_API_KEY` and
  checks the key is not in the result.

Arguments are strict: an unknown name is refused rather than dropped, a missing required one is
refused, and tickers, dates, horizons and enums go through the validators the routes use. A
refused call is an `isError` result whose text starts with the tool name and says why, and the
tests show it reaches no route, provider or model. Null counts as absent.

`ask_lattice_animals` is the library's own tool, so it follows the library: it checks the question
and `to` before the call goes out (an empty question or an unknown `to` is an error result and
nothing is sent), and it forwards only those two, so an extra argument such as a URL is dropped,
not sent (a test shows the outbound arguments).

Text that leaves the server is scrubbed: the value of any environment variable whose name
contains `KEY`, `TOKEN`, `SECRET` or `PASSWORD`, and `apiKey=...` query parameters, are replaced
with `[redacted]`. An error message also has URLs and `host='...'` replaced, because a
transport error can carry the inference endpoint (the existing `/api/tabular/peer-forecast`
route returns such a message as its `503` reason).

### Not offered

Not on the list, so `tools/call` answers `-32602`: `analyze_ticker`, `analyze_batch`, `stock_fax`,
`vision_memo` (language model calls); `situate`, `prism_memo`, `situate_chat`, `prism_chat`,
`situate_export`, `prism_export` (builds, chat and exports); `ticker_research_bundle` (unbounded
output, and a route with per-process and per-client admission limits, of which the per-client one
does not apply to in-process calls, since they look like loopback); `render_chart`,
`torque_score`, `moneyline`, `pixel_image` (images); `torque_scan`, `watchlist_cockpit`,
`watchlist_alerts`, `resolve_watchlist` (lists and URLs); `search_news`,
`compose_research_article`. Also not exposed: `/api/tabular/predict`, `/api/agent/*`,
`/api/alerts/*`, `/api/data/market/*`, `/api/citations/classify`, watchlists and studio, and
anything with a user id or a key. No tool takes a path, a URL or an id beyond a ticker.

The tools build their request paths from validated arguments, and the tests record every route
they reach in process: `/api/health`, `/api/providers`, `/api/sec/{ticker}`,
`/api/data/charts/{pack}`, `/api/data/tools/torque`, `/api/data/tools/moneyline`, the two
`/summary` reads and `/api/tabular/peer-forecast/{ticker}`. None is a build, a chat, an export or
`/api/data/ticker-research`.

### The hop rule

Every call between sites carries `x-mcp-hop` (how many sites have already relayed it; the older
`x-lattice-hop` counts too) and `x-mcp-path` (their names). A site makes an outbound call only
while the hop is under 2, and sends hop + 1 with its own name added to the path.

- `ask_lattice_animals` at hop 2 is refused before it runs, with a result that starts `too-deep`.
  At hop 0 the outbound carries `x-mcp-hop: 1` and `x-mcp-path: underlying-analyzer`; at hop 1
  it carries `2` and `<caller>>underlying-analyzer`.
- The other tools do not call out, so they still answer at hop 2.
- `POST /mcp/lattice` at hop 2 is JSON-RPC error `-32001`.

`tests/test_constellation_mcp.py` shows each of these against a stub peer on `127.0.0.1`. The
counter is checked by each member; nothing signs it. Outbound calls also share a daily cap
(500) and each caller address has a limit of 40 calls a minute; both are per process, and the
service runs three gunicorn workers.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `LATTICE_MCP_URL` | `https://latticeanimal-production.up.railway.app/mcp` | The hub's MCP, which `ask_lattice_animals` and `POST /mcp/lattice` reach. Must be https. |
| `MCP_PUBLIC_ORIGIN` | `https://underlying-terminal-production.up.railway.app` | The origin the manifest advertises as `endpoint`. |
| `MCP_ALLOW_LOCAL` | unset | Set to `1` to let `LATTICE_MCP_URL` be plain `http` on loopback, for a laptop or a test. |

### Try it

```bash
curl -s -X POST http://127.0.0.1:5050/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools[].name'

curl -s -X POST http://127.0.0.1:5050/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"chart_data","arguments":{"chart_type":"auction","ticker":"AAPL"}}}'

curl -s http://127.0.0.1:5050/.well-known/mcp.json | jq '.endpoint, .peers'
```

## HTTP docs

- Site: `/docs`, `/docs#mcp`, `/docs#api`
- Markdown: `/docs/api.md`
- Catalog JSON: `/api/docs`
- OpenAPI: `/api/openapi`
- Constellation manifest: `/.well-known/mcp.json`
