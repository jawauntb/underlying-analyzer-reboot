# API Reference

Base URL (local): `http://127.0.0.1:5050`  
Production: `https://underlying-terminal-production.up.railway.app`

Public access: research and tool endpoints require **no API key**. CORS is open.  
Machine-readable catalog: `GET /api/docs` · OpenAPI 3.1: `GET /api/openapi` · Site docs: `/docs`
MCP: [mcp.md](mcp.md) (streamable HTTP at `POST /api/mcp`) · Agent: [agent.md](agent.md)

Every agent/MCP tool is a thin binding over one of the routes below, declared once in
`app/tool_registry.py`. The catalog, the OpenAPI document, both MCP transports, and the
`/chat` agent are all generated from it.

All JSON request bodies use `Content-Type: application/json`. Errors return:

```json
{ "error": "human-readable message" }
```

Typical status codes: `200` success, `400` bad input / data failure, `401` auth required, `500` unexpected server error, `503` scheduler misconfigured.

Ticker selection (charts, analysis, cockpit, alerts, torque scan) accepts one of:

| Field | Type | Notes |
| --- | --- | --- |
| `ticker` | string | Single symbol, e.g. `"AAPL"` |
| `tickers` | string or string[] | Comma-separated string or array |
| `watchlist_url` | string | Public TradingView watchlist URL (takes precedence) |
| `max_results` | int | Default `10`, cap `50` |

---

## Health & config

### `GET /api/docs`

Public catalog of site tools and HTTP endpoints.

```bash
curl -s http://127.0.0.1:5050/api/docs
```

### `GET /api/health`

Liveness check (Railway healthcheck).

```bash
curl -s http://127.0.0.1:5050/api/health
```

```json
{ "ok": true, "service": "underlying-analyzer-reboot" }
```

### `GET /api/config`

Public client config. Never returns service-role secrets.

```bash
curl -s http://127.0.0.1:5050/api/config
```

```json
{
  "supabase": {
    "enabled": true,
    "url": "https://your-project.supabase.co",
    "anon_key": "public-anon-key"
  }
}
```

### `GET /api/providers`

Market data provider and routing status. The current response remains valid; additive fields
must never contain `MASSIVE_API_KEY` or another secret.

```bash
curl -s http://127.0.0.1:5050/api/providers
```

```json
{
  "primary": "massive",
  "fallback": "yfinance + nasdaq",
  "massive_configured": true,
  "fallback_enabled": true,
  "freshness": {
    "stocks": "Plan-dependent: end-of-day, 15-minute delayed, or real-time",
    "options": "Plan-dependent: unavailable, 15-minute delayed, or real-time"
  },
  "notes": [
    "Massive is the primary provider when MASSIVE_API_KEY is configured.",
    "Legacy yfinance and Nasdaq routing is temporary and controlled by MARKET_DATA_FALLBACK_ENABLED.",
    "Massive coverage and freshness depend on the subscribed Stocks and Options plans."
  ]
}
```

`massive_configured` is the sanitized state of whether `MASSIVE_API_KEY` is present;
`fallback_enabled` is the sanitized state of `MARKET_DATA_FALLBACK_ENABLED`. An unavailable entitlement, timeout, 429, 5xx,
or empty Massive result may route to the next provider only when fallback is enabled. An
invalid request is not retried as an upstream outage.

### `GET /api/data/market/stream` (additive)

Streams Massive stock or option events as `text/event-stream`. The deployed Flask service keeps
the public transport as SSE while the server maintains the upstream Massive WebSocket.

```bash
curl -N 'http://127.0.0.1:5050/api/data/market/stream?ticker=AAPL&feed=trades'
```

Query parameters: `ticker` (required), `asset_class` (`stocks` or `options`, default `stocks`),
`feed` (`trades`, `quotes`, `aggregates_minute`, or `aggregates_second`, default `trades`), and
optional `max_events` (1–1000, useful for smoke tests). Events are named `ready`, `market_data`,
and `error`; each data event includes `provider`, `feed`, `ticker`, `timestamp`, `timestamp_ms`,
and the additive `data` payload. A stream entitlement failure is an `error` event with code
`not_entitled`; it is never silently served by yfinance.

The additive raw-data routes are cataloged by `GET /api/docs` and `GET /api/openapi`: market
snapshot, all-stock snapshot, aggregates, trades, quotes, status, news, IPOs, conditions,
TMX corporate events, options chain/expirations/contracts, single-contract snapshots, ticker
events, dividends, splits, and selected financial statements/ratios. They use a stable
`{ticker, provider, provider_note, data}` envelope (market status omits `ticker`) and never
replace an existing endpoint.

### `GET /api/capabilities` (additive)

Returns the declared Massive capability matrix and freshness hints. It is an entitlement
preflight, not a live authorization check; request-time 401/403 responses remain authoritative,
and the explicit legacy fallback may still serve a compatible request. This endpoint is additive
to the existing catalog and does not replace `/api/docs`, `/api/openapi`, or `/api/providers`.

```json
{
  "provider": "massive",
  "capabilities": [
    {
      "id": "stocks.daily_bars",
      "available": true,
      "freshness": "delayed_15m",
      "history": "10y",
      "plan": "developer",
      "reason": null
    },
    {
      "id": "options.chain",
      "available": false,
      "plan": null,
      "reason": "Massive plan is not declared; entitlement must be verified"
    }
  ]
}
```

Every item has a stable `id`, `available` boolean, `plan`, and freshness/history hints. The
`reason` is sanitized and may explain missing credentials, undeclared entitlements, or a
dedicated partner subscription. Capability IDs are additive: clients must ignore
unknown future IDs and must not infer access from the provider name alone.

### `GET /api/capabilities/<capability>` (additive)

Returns the same capability object for one ID, or `404 {"error":"unknown capability"}` for an
ID the server does not recognize. It is intended for a UI to gate optional controls before
calling `/api/data/...`; it does not alter the request or response contract of those routes.

---

## Charts

### `POST /api/charts/<chart_type>`

Supported `chart_type` values:

| Type | Purpose | Extra body fields |
| --- | --- | --- |
| `auction` | Auction levels | `period` (default `1y`), `interval` (`15m`/`1d`/`1w`) |
| `performance` | Monthly seasonality | `month` (1–12, default `1`) |
| `regression` | Regression channel | `period`, `start_date`, `end_date`, `interval` |
| `ridge-growth` | Ridge strategy pack (6mo/1y/2y) | ticker/watchlist only |
| `flow-compass` | Flow indicator dashboard | `period` (default `1y`), `interval` |
| `torque` | Torque inflection chart | `period` (default `2y`), `interval` |
| `portfolio` | Multi-ticker portfolio | `investment_per_stock`, `benchmark`, `start_date`, `end_date` |
| `volatility` | Cross-ticker vol compare | ticker/watchlist only |

Underscores are accepted (`ridge_growth` → `ridge-growth`).

`period` accepts `5d` (≈ one trading week), `1mo`, `3mo`, `6mo`, `1y`, `2y`,
`5y`, `10y` wherever a chart type takes a period.

`interval` accepts `15m`, `1d` (default), and `1w`. Chart history uses Massive
aggregates at that bar size and overlays the current snapshot on the last bar
so quotes are live minus exchange delay. `15m` clamps long lookbacks to `5d`
unless `1d` or `1mo` is requested.

**Request**

```bash
curl -s -X POST http://127.0.0.1:5050/api/charts/auction \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"AAPL","period":"1y"}'
```

**Watchlist batch**

```bash
curl -s -X POST http://127.0.0.1:5050/api/charts/portfolio \
  -H 'Content-Type: application/json' \
  -d '{
    "watchlist_url":"https://www.tradingview.com/watchlists/334089913/",
    "max_results":5,
    "investment_per_stock":100
  }'
```

**Response shape (common)**

```json
{
  "images": [
    { "filename": "aapl-auction-1y.png", "mime": "image/png", "data": "<base64>" }
  ],
  "provider": "yfinance",
  "provider_note": "...",
  "meta": {},
  "export": {
    "generated_at": "2026-08-05T18:00:00+00:00",
    "mode": "auction",
    "tickers": ["AAPL"],
    "image_files": [{ "filename": "aapl-auction-1y.png", "mime": "image/png" }]
  }
}
```

---

## Chart data (for upstream UIs)

Existing `/api/charts/...` and image tool routes are unchanged. Prefer these
`/api/data/...` routes when your app will draw charts itself.

Full rendering guide (per-chart payload schemas + the terminal visual style
rules for native UIs): [chart-data-rendering.md](chart-data-rendering.md),
served at `GET /docs/chart-data-rendering.md`.

### `POST /api/data/charts/<chart_type>`

Same `chart_type` values and request body as `/api/charts/<chart_type>`. Returns
`datasets` (series / levels / tables / meta) instead of `images`.

```bash
curl -s -X POST http://127.0.0.1:5050/api/data/charts/auction \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"AAPL","period":"1y"}'
```

**Response shape (common)**

```json
{
  "datasets": [
    {
      "chart_type": "auction",
      "ticker": "AAPL",
      "meta": { "vah": 1.0, "val": 1.0, "poc": 1.0 },
      "levels": { "vah": 1.0, "val": 1.0, "poc": 1.0 },
      "series": { "ohlcv": [], "close": [] }
    }
  ],
  "provider": "yfinance",
  "provider_note": "...",
  "meta": {},
  "export": { "mode": "auction-data", "tickers": ["AAPL"], "image_files": [] }
}
```

### `POST /api/data/tools/torque`

Torque score + chartable price/fundamental series (no PNG).

```bash
curl -s -X POST http://127.0.0.1:5050/api/data/tools/torque \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"AAPL"}'
```

### `POST /api/data/tools/moneyline`

Options open-interest ladder as JSON (no PNG).

```bash
curl -s -X POST http://127.0.0.1:5050/api/data/tools/moneyline \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"AAPL"}'
```

### Stable provider metadata

Existing chart routes retain their top-level `provider`, `provider_note`, `meta`, `datasets`,
`series`, and `export` keys. The new raw-data routes return those same provider fields plus a
`data` object containing the provider-shaped payload. `data_meta` is reserved as an optional
future field for source timestamps and routing provenance; clients must not infer freshness from
the provider name alone.

| Field | Meaning |
| --- | --- |
| `provider` | Provider that produced this result: `massive`, `yfinance`, `nasdaq`, or `mixed` |
| `freshness` | `end_of_day`, `delayed_15m`, `realtime`, `updated_daily`, or `plan_dependent` |
| `as_of` | ISO-8601 timestamp of the newest source observation, when available |
| `retrieved_at` | ISO-8601 timestamp when the server received the result |
| `fallback_used` | Boolean indicating that routing left the preferred provider |
| `fallback_reason` | Sanitized reason such as `timeout`, `rate_limited`, `not_entitled`, or `not_configured` |

For batch routes, the top-level provider describes the batch (`mixed` when providers differ).
Freshness is plan-dependent and is exposed through `/api/providers` and `/api/capabilities`; a
missing source timestamp is not permission to guess freshness.

---

## Analysis / briefs

### `GET /api/analysis/<ticker>`

Single-ticker summary + Anthropic brief.

```bash
curl -s http://127.0.0.1:5050/api/analysis/AAPL
```

Both analysis routes also return `market_context`: the chart data behind the
auction, torque, and moneyline charts compressed into numbers — value area
(VAH/VAL/POC) and acceptance, torque stage/score/components with the latest
EMA and SMA readings, and options open-interest positioning. The brief is
generated with that context, so the prose can cite levels instead of guessing
at them. Every source is best effort: anything missing is named in the entry's
`unavailable` list rather than dropped, and a batch builds context for at most
five resolved tickers.

### `POST /api/analysis`

Batch analysis for tickers or a watchlist.

```bash
curl -s -X POST http://127.0.0.1:5050/api/analysis \
  -H 'Content-Type: application/json' \
  -d '{"tickers":["AAPL","MSFT"],"max_results":10}'
```

Requires `ANTHROPIC_API_KEY` for generated brief text.

---

## Watchlists

### `POST /api/watchlists/resolve`

Resolve a public TradingView watchlist to tickers.

```bash
curl -s -X POST http://127.0.0.1:5050/api/watchlists/resolve \
  -H 'Content-Type: application/json' \
  -d '{
    "watchlist_url":"https://www.tradingview.com/watchlists/334089913/",
    "max_results":10
  }'
```

```json
{
  "watchlist": { "name": "...", "url": "...", "tickers": ["AAPL", "MSFT"] },
  "tickers": ["AAPL", "MSFT"],
  "max_results": 10
}
```

### `POST /api/watchlists/cockpit`

Ranked cockpit table (lane, scanner score, ridge, flow, auction).

```bash
curl -s -X POST http://127.0.0.1:5050/api/watchlists/cockpit \
  -H 'Content-Type: application/json' \
  -d '{"tickers":["AAPL","MSFT"],"max_results":10}'
```

Partial symbol failures are returned in `meta.errors`; if every symbol fails, status is `400`.

### `POST /api/watchlists/alerts`

Alert digest over a ticker set or watchlist.

| Field | Type | Notes |
| --- | --- | --- |
| `max_alerts` | int | Default `12` |
| `volatility_threshold` | float | Optional severity threshold |
| `period` | string | History window when applicable |

```bash
curl -s -X POST http://127.0.0.1:5050/api/watchlists/alerts \
  -H 'Content-Type: application/json' \
  -d '{"tickers":["AAPL","MSFT"],"max_alerts":5}'
```

Response includes `alerts`, `digest`, `rows`, `meta`, and `export`.

---

## Alerts (scheduler / webhooks)

### `GET /api/alerts/scheduler/status`

```bash
curl -s http://127.0.0.1:5050/api/alerts/scheduler/status
```

```json
{
  "configured": true,
  "service_role_configured": true,
  "token_configured": true,
  "schedule": "daily"
}
```

### `POST /api/alerts/scheduled/run`

Run saved daily alert rules. Requires:

```http
Authorization: Bearer <ALERT_SCHEDULER_TOKEN>
```

Body (all optional):

```json
{
  "run_date": "2026-08-05",
  "force": false,
  "limit": 50
}
```

```bash
curl -s -X POST http://127.0.0.1:5050/api/alerts/scheduled/run \
  -H "Authorization: Bearer $ALERT_SCHEDULER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{}'
```

- Missing/invalid token → `401`
- Token env not configured → `503`

### `POST /api/alerts/webhook/test`

Send a test webhook for one saved rule. Requires a Supabase user bearer token:

```http
Authorization: Bearer <supabase_access_token>
```

```bash
curl -s -X POST http://127.0.0.1:5050/api/alerts/webhook/test \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"rule_id":"rule-uuid","run_date":"2026-08-05"}'
```

---

## SEC

### `GET /api/sec/<ticker>`

SEC EDGAR source pack (filings metadata, excerpts, XBRL facts when available).

```bash
curl -s http://127.0.0.1:5050/api/sec/AAPL
```

Set a descriptive `SEC_USER_AGENT`. See README for rate-limit guidance.

---

## Tools

### `POST /api/tools/fax`

Stock Fax narrative pack.

```bash
curl -s -X POST http://127.0.0.1:5050/api/tools/fax \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"AAPL"}'
```

### `POST /api/tools/vision`

Classic Market Memo (JSON).

```bash
curl -s -X POST http://127.0.0.1:5050/api/tools/vision \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"AAPL"}'
```

### `POST /api/tools/vision/stream`

Classic memo as NDJSON stream (`application/x-ndjson`).

### `POST /api/tools/vision/v2`

Vision v2 memo (SEC + optional Exa enrichment).

```bash
curl -s -X POST http://127.0.0.1:5050/api/tools/vision/v2 \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"AAPL"}'
```

### `POST /api/tools/vision/v2/stream`

Vision v2 phased NDJSON stream.

### `POST /api/tools/vision/v2/pdf`

PDF download (`application/pdf`).

| Field | Required | Notes |
| --- | --- | --- |
| `ticker` | yes | Symbol |
| `memo_text` | no | Reuse existing memo text |
| `report` | no | Full report object when reusing memo |
| `charts` | no | Chart image payloads; otherwise regenerated |

```bash
curl -s -X POST http://127.0.0.1:5050/api/tools/vision/v2/pdf \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"AAPL"}' \
  -o AAPL-vision-memo.pdf
```

### `POST /api/tools/torque`

Single-ticker torque score + chart.

```bash
curl -s -X POST http://127.0.0.1:5050/api/tools/torque \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"AAPL"}'
```

### `POST /api/tools/torque/scan`

Batch torque scan over tickers/watchlist (`max_results` default `10`, cap `50`).

```bash
curl -s -X POST http://127.0.0.1:5050/api/tools/torque/scan \
  -H 'Content-Type: application/json' \
  -d '{"tickers":["AAPL","MSFT"],"max_results":10}'
```

### `POST /api/tools/torque/scan/stream`

NDJSON stream of torque scan rows.

### `POST /api/tools/moneyline`

Options moneyline / moneywall chart.

```bash
curl -s -X POST http://127.0.0.1:5050/api/tools/moneyline \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"AAPL","expiry":"2026-06-19"}'
```

`expiry` is optional (YYYY-MM-DD).

### `POST /api/tools/pixel`

Generate a Pixel image from a prompt. Requires `OPENAI_API_KEY`.

```bash
curl -s -X POST http://127.0.0.1:5050/api/tools/pixel \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"market mascot in neon terminal style"}'
```

---

## Streaming responses

Stream endpoints return `application/x-ndjson`: one JSON object per line. Clients should read incrementally and parse each line independently.

Affected routes:

- `POST /api/tools/vision/stream`
- `POST /api/tools/vision/v2/stream`
- `POST /api/tools/torque/scan/stream`

---

## Legacy compatibility routes

Kept for older frontends. Prefer `/api/...` above.

| Method | Path | Maps to |
| --- | --- | --- |
| `POST` | `/plot-auction-levels` | `/api/charts/auction` |
| `POST` | `/plot-performance` | `/api/charts/performance` |
| `POST` | `/plot-regression` | `/api/charts/regression` |
| `POST` | `/plot-portfolio-performance` | `/api/charts/portfolio` |
| `POST` | `/plot-volatility` | `/api/charts/volatility` |
| `GET` | `/stock_analysis/<ticker>` | Stock Fax |
| `GET` | `/micro_memo/<ticker>` | Classic Vision memo |
| `POST` | `/generate-image` | Pixel |
| `POST` | `/plot-moneylines` | Moneyline |
| `POST` | `/plot-moneywall` | Moneyline |

---

## Agent

See [agent.md](agent.md) for the streaming protocol and event vocabulary.

### `GET /api/agent/tools`

Capability catalog the agent routes against: name, group, when-to-use guidance, cost hint,
HTTP binding, and argument names. Also reports `agent_ready`.

### `POST /api/agent/chat/stream`

Run one agent turn as an NDJSON event stream (`application/x-ndjson`).

```json
{
  "messages": [{ "role": "user", "content": "How does NVDA look this week?" }],
  "tools": ["render_chart", "search_news"],
  "tool_policy": "exact",
  "context": "optional extra system context"
}
```

`messages` is required. For backwards compatibility, `tools` is a best-effort
allowlist when `tool_policy` is omitted: unrecognized entries are ignored and a
request with no recognized names falls back to every agent tool. Set
`tool_policy` to `"exact"` to require a non-empty allowlist made entirely of
recognized names; invalid exact requests return `400`.
Events: `start`, `text`, `tool_call`, `tool_result`, `article`, `error`, `done`.

```bash
curl -N -X POST http://127.0.0.1:5050/api/agent/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"How does NVDA look?"}]}'
```

### `POST /api/agent/chat`

Same turn, folded into one JSON body: `text`, `tool_calls`, `tool_trace`, `artifacts`,
`articles`, `stop_reason`.

### `POST /api/agent/article`

Validate and normalize a research article. Requires `title`, `thesis`, and `sections`;
optional `subtitle`, `tickers`, `recommendations`, `risks`, `sources`. Returns the
normalized `article`, rendered `markdown`, and a short `summary`.

---

## MCP

### `GET /api/mcp`

Server descriptor: transport, protocol version, tool count, supported methods.

### `POST /api/mcp`

Streamable HTTP MCP endpoint speaking JSON-RPC 2.0. Stateless, no API key.

```bash
curl -s -X POST http://127.0.0.1:5050/api/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Methods: `initialize`, `ping`, `tools/list`, `tools/call`, `resources/list`,
`resources/read`, `prompts/list`. Send `Accept: text/event-stream` to receive the response
as a single SSE frame. Notifications return `202` with no body.

---

## News

### `POST /api/news`

Recent news and web results for a ticker and/or topic. Requires `EXA_API_KEY`; without it
the route returns `ok: false` with `status: "not configured"` rather than failing.

```json
{ "ticker": "NVDA", "query": "data center capex", "days_back": 14, "num_results": 6 }
```

---

## Auth summary

| Endpoint | Auth |
| --- | --- |
| Most `/api/*` | None (server-side market/LLM keys) |
| `/api/alerts/scheduled/run` | `Bearer ALERT_SCHEDULER_TOKEN` |
| `/api/alerts/webhook/test` | Supabase user access token |
| Browser Library / saved rules | Supabase anon key via `/api/config` + client auth |

---

## Env keys used by the API

| Variable | Used for |
| --- | --- |
| `ANTHROPIC_API_KEY` | Briefs, Fax, Vision memos, and the research agent |
| `ANTHROPIC_TEXT_MODEL` | Text model override |
| `ANTHROPIC_AGENT_MODEL` | Agent model override (defaults to `ANTHROPIC_TEXT_MODEL`) |
| `OPENAI_API_KEY` | Pixel images |
| `OPENAI_IMAGE_MODEL` | Image model override |
| `SEC_USER_AGENT` | SEC EDGAR polite access |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` | Public config + client auth |
| `SUPABASE_SERVICE_ROLE_KEY` | Scheduled alert runs |
| `ALERT_SCHEDULER_TOKEN` | Cron/Function → scheduled run |
| `EXA_API_KEY` | Vision v2 enrichment and `/api/news` (optional) |

### Massive Doppler variables

These are names only; values belong in Doppler and are intentionally omitted from this document.
`MASSIVE_API_KEY` is server-only and must never appear in `/api/config`, `/api/providers`, logs,
or capability responses.

| Variable | Purpose |
| --- | --- |
| `MASSIVE_API_KEY` | Server-side Massive authentication secret |
| `MASSIVE_REST_BASE_URL` | Massive REST base URL; default `https://api.massive.com` |
| `MASSIVE_TIMEOUT_SECONDS` | Per-request timeout budget |
| `MASSIVE_MAX_RETRIES` | Bounded retry count for transient failures |
| `MASSIVE_MAX_PAGES` | Maximum pagination pages to collect from one request |
| `MARKET_DATA_FALLBACK_ENABLED` | Permit routing to yfinance/Nasdaq after a Massive failure or entitlement miss |
| `MASSIVE_STREAM_ENABLED` | Enable the additive Massive WebSocket/SSE stream |
| `MASSIVE_WS_BASE_URL` | WebSocket base URL; default `wss://socket.massive.com` |
| `MASSIVE_STREAM_TIMEOUT_SECONDS` | WebSocket connect/read timeout |
| `MASSIVE_STREAM_MAX_RECONNECTS` | Maximum bounded reconnect attempts |
| `MASSIVE_STOCKS_PLAN` | Sanitized plan declaration used for capability reporting: `basic`, `starter`, `developer`, or `advanced` |
| `MASSIVE_OPTIONS_PLAN` | Sanitized Options plan declaration: `basic`, `starter`, `developer`, or `advanced` |
| `MASSIVE_FINANCIALS_PLAN` | Sanitized financials/ratios entitlement declaration, including any expansion plan |

The plan variables are configuration hints, not a substitute for an authorization response from
Massive. `/api/capabilities` reports declared-plan availability; a live 401/403 or provider error
still wins at request time and routes to the explicit fallback when enabled.
