# The Underlying Analyzer Reboot

A reboot of the old `tube` Python chart backend and `tufe` frontend as one repo:

- Flask API for chart generation and stock summaries
- Static frontend with the original retro terminal styling
- A Massive-first market-data adapter with yfinance and Nasdaq fallbacks
- Additive Massive stock/options streaming through `/api/data/market/stream`
- Nasdaq public historical fallback for daily US equity OHLCV when keyed providers fail
- Public TradingView watchlist links for portfolio, chart batches, volatility, and stock briefs
- SEC EDGAR source packs for filings, XBRL company facts, and Vision memo citations
- Supabase-backed saved research, watchlists, daily alert rules, and alert-run inbox
- A research agent at `/chat` with streaming answers, inline charts, and saveable briefs
- One tool registry driving the HTTP API, OpenAPI 3.1, both MCP transports, and the agent
- JSON exports for generated ticker/watchlist data

## Native iPhone app

[`mobile/`](mobile/) contains Undercurrent, the Expo Go iPhone companion. Green `Mobile cloud proof` on `main` cuts TestFlight. Setup, native test commands, static export checks, production contract smoke, and cloud-proof boundaries are documented in [`mobile/README.md`](mobile/README.md).

## Data Provider Notes

`yfinance` still works for many people, but it is unofficial Yahoo Finance access. Recent
failures have centered on unauthorized cookie/crumb responses and undocumented throttling.
The production path makes Massive the first keyed adapter for supported stock and
options capabilities, while preserving yfinance and the existing Nasdaq daily-US-equity
fallback. Provider-specific response shapes must not leak into the chart or analysis APIs.

### Massive-first provider path

The adapter is implemented behind `MarketDataClient`:

```text
Massive (when enabled and entitled)
  -> yfinance (if fallback is enabled and Massive is unavailable)
    -> Nasdaq daily US-equity fallback (if yfinance also fails)
```

Massive is selected when `MASSIVE_API_KEY` is configured. `MARKET_DATA_FALLBACK_ENABLED` controls
whether a Massive timeout, rate limit, authorization/entitlement response, empty result, or upstream error may
continue to the existing providers. Invalid request parameters remain client errors; every
successful provider is normalized to the same OHLCV, options, metadata, and error contracts.

Massive credentials belong in Doppler. Documentation names the variables but never includes
their values: `MASSIVE_API_KEY`, `MASSIVE_REST_BASE_URL`, `MASSIVE_TIMEOUT_SECONDS`,
`MASSIVE_MAX_RETRIES`, `MASSIVE_MAX_PAGES`, and the routing flag
`MARKET_DATA_FALLBACK_ENABLED`. Streaming additionally uses
`MASSIVE_STREAM_ENABLED`, `MASSIVE_WS_BASE_URL`, `MASSIVE_STREAM_TIMEOUT_SECONDS`, and
`MASSIVE_STREAM_MAX_RECONNECTS`; it is a Server-Sent Events endpoint backed by Massive's
WebSocket connection so the current WSGI/Gunicorn deployment remains compatible. Capability
reporting may additionally use
`MASSIVE_STOCKS_PLAN` and `MASSIVE_OPTIONS_PLAN`. Do not return `MASSIVE_API_KEY` from
`/api/config`, `/api/providers`, or any capability catalog. See [SYSTEM_DESIGN.md](SYSTEM_DESIGN.md)
for the adapter boundary, stable metadata envelope, entitlement rules, and rollout sequence.

Massive plan access is endpoint-specific. Its stock custom-bars docs list Basic as end-of-day
with two years, Starter as 15-minute delayed with five years, Developer as 15-minute delayed
with ten years, and Advanced as real-time with all history. Tick-level stock trades are not
included on Basic or Starter, are 15-minute delayed with ten years on Developer, and real-time
with all history on Advanced. The app exposes plan-based freshness hints through
`/api/providers` and `/api/capabilities`; it does not synthesize realtime timestamps when the
upstream response does not provide them. See the [Massive REST
quickstart](https://massive.com/docs/rest/quickstart), [stock custom bars](https://massive.com/docs/rest/stocks/aggregates/custom-bars),
and [stock trades](https://massive.com/docs/rest/stocks/trades-quotes/trades) docs.

Options are a separate subscription family. Contract reference data is documented as included
in all Options plans, while bars, snapshots, trades, and quotes are available only on select
Options plans. Native ticker events are experimental and currently expose `ticker_change`; the
partner API is separately subscribed data, so a TMX corporate-events or Benzinga response must
not be implied by a Stocks or Options subscription. See the [Options overview](https://massive.com/docs/rest/options/overview),
[ticker events](https://massive.com/docs/rest/stocks/corporate-actions/ticker-events), and
[Partners overview](https://massive.com/docs/rest/partners/overview).

Additive Massive routes also expose stock market status, dividends, splits, and selected
financial statements/ratios. Financials use a separate `MASSIVE_FINANCIALS_PLAN` declaration;
the capability endpoint reports them as unavailable until that entitlement is explicitly known.

The entitlement-aware raw-data surface also includes `/api/data/market/news/<ticker>`,
`/api/data/market/corporate-events`, `/api/data/market/ipos`,
`/api/data/market/conditions`, `/api/data/market/snapshot/all`, and
`/api/data/options/<ticker>/snapshot/<contract>`. These are additive envelopes and do not
change the existing chart or options-chain schemas. The current subscription evidence is
Stocks Advanced, Options Developer, Financials & Ratios, and TMX Corporate Events; the
runtime still reports plan hints as unverified until the corresponding optional plan variables
are deliberately configured, and a live Massive 401/403 remains authoritative.

### Realtime stream

`GET /api/data/market/stream?ticker=AAPL&feed=trades` returns `text/event-stream` events. The
server connects to Massive's `wss://socket.massive.com/stocks` feed, authenticates with the
server-side key, and forwards sanitized `ready`, `market_data`, and `error` events. Use
`asset_class=options` with a Massive option contract ticker (for example,
`O:SPY241220P00720000`) for the options feed. `quotes`, `aggregates_minute`, and
`aggregates_second` are also supported. An Advanced Stocks entitlement is required for
real-time stock delivery. Massive may still reject a key with an entitlement error, which is
reported as a sanitized stream error and never silently replaced with yfinance.

Free keyed options worth considering later:

- Financial Modeling Prep: 250 calls/day on the free personal plan.
- Twelve Data: free basic plan with 8 credits/minute and 800/day.
- Alpha Vantage: free, but currently only 25 requests/day.
- Marketstack: free, but 100 requests/month and one year of EOD history.

Vision also uses the SEC EDGAR APIs for 10-K/10-Q/8-K metadata, filing excerpts, and XBRL
company facts. SEC APIs are free and do not require an API key, but automated clients should
declare a descriptive `SEC_USER_AGENT` and keep request volume modest. SEC fair-access guidance
currently caps automated access at 10 requests/second across machines; this app defaults to a
more conservative per-process interval, retry backoff, and in-memory SEC source-pack/URL caches so
watchlist and repeated Vision runs do not refetch the same filing payloads.
If SEC blocks the runtime, the app falls back to Yahoo-hosted SEC filing copies for 10-K/10-Q/8-K
sections; XBRL company facts are only available from the direct SEC API path.

## Research Agent

`/chat` is a conversational console over every capability in the terminal. It streams
answers, renders the charts it generates inline, remembers past conversations, and can
publish a saveable research brief with explicit recommendations and invalidation
conditions.

Every capability is declared once in [`app/tool_registry.py`](app/tool_registry.py).
Five surfaces are generated from that single declaration, so they cannot drift apart:

| Surface | Endpoint |
| --- | --- |
| Machine-readable catalog | `GET /api/docs` |
| OpenAPI 3.1 | `GET /api/openapi` |
| MCP over streamable HTTP | `POST /api/mcp` |
| MCP over stdio | `underlying-mcp` |
| The agent | `/chat`, `POST /api/agent/chat/stream` |

Tools execute in-process against the app's own public HTTP routes, so there is exactly one
implementation of each capability and no network hop between the agent and the API.
Rendered charts are lifted out of tool results as artifacts: the model reads a cheap
reference, the browser receives the image and renders it inline.

Set `ANTHROPIC_API_KEY` to enable the agent, and optionally `ANTHROPIC_AGENT_MODEL` to run
the conversation on a different model from memo generation. Conversations and briefs save
to Supabase when signed in and to local storage otherwise.

See [docs/agent.md](docs/agent.md) for the streaming protocol.

## Docs

On-site docs: `/docs` (API section at `/docs#api`, MCP at `/docs#mcp`).

Machine-readable catalog: `GET /api/docs` (lists every public endpoint, tool, and site tool).

OpenAPI 3.1: `GET /api/openapi`.

Raw markdown API reference: `/docs/api.md` and [docs/api.md](docs/api.md).

MCP (no API key): [docs/mcp.md](docs/mcp.md). Streamable HTTP at `/api/mcp`, or stdio via
`underlying-mcp` pointed at the Railway production URL by default.

## Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m flask --app app.main run --port 5050
```

Open `http://127.0.0.1:5050`.

For text/report generation and Pixel image generation, copy `.env.example` to `.env` and paste
your keys. Anthropic powers stock briefs, Stock Fax narratives, and Market Memo text. OpenAI is
used only for Pixel image generation.

```bash
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_TEXT_MODEL=claude-opus-4-8
ANTHROPIC_AGENT_MODEL=
OPENAI_API_KEY=sk-proj-...
OPENAI_IMAGE_MODEL=gpt-image-2
SEC_USER_AGENT="The Underlying Analyzer Reboot contact:jawauntb@users.noreply.github.com"
SEC_REQUEST_INTERVAL_SECONDS=0.35
SEC_SOURCE_PACK_CACHE_SECONDS=21600
SEC_RESPONSE_CACHE_SECONDS=86400
SEC_MAX_RETRIES=2
```

Restart the Flask process after changing `.env`.

## Watchlist Workflow

Paste a public TradingView watchlist URL, set `Max results`, and generate any mode that accepts
tickers. Portfolio and Volatility combine the resolved symbols into one chart. Auction, Month Map,
Regression, and Brief generate one result per resolved ticker. The `Export JSON` button downloads
the structured result data, including resolved tickers, watchlist metadata, per-symbol metrics, and
any skipped-symbol errors.

Signed-in users can save manual or TradingView watchlists from the terminal form. Saved watchlists
now include a compact cockpit queue: `Rank` expands the saved list into a top-name table with lane,
score, Flow Compass, Ridge, and auction location, while `Cockpit` and `Alerts` launch full runs from
that saved list.

The signed-in Library also includes ticker timelines. Enter a ticker, or leave the field empty to
use the current form ticker, then open `Timeline` to review prior saved runs and the latest
headline changes against the previous saved run.

## Railway Deploy

Railway is the production host for this Flask app. The Procfile starts Gunicorn on Railway's
provided `$PORT`. `railway.toml` mirrors that start command, configures `/api/health` as the
deployment healthcheck, and forces the Railpack builder. `requirements.txt` gives Railway's pip
installer an explicit dependency file, while `.python-version` keeps production on Python 3.12.
`pyproject.toml` pins package discovery to the `app` package so Railway's Python installer does not
accidentally treat migration folders as import packages.

```bash
railway link
railway variable set \
  OPENAI_API_KEY=... \
  ANTHROPIC_API_KEY=... \
  ANTHROPIC_TEXT_MODEL=claude-opus-4-8 \
  OPENAI_IMAGE_MODEL=gpt-image-2 \
  SUPABASE_URL=... \
  SUPABASE_ANON_KEY=... \
  SUPABASE_SERVICE_ROLE_KEY=... \
  ALERT_SCHEDULER_TOKEN=... \
  SEC_USER_AGENT=...
railway up
railway domain
```

For magic links, add the Railway URL to Supabase Auth redirect URLs alongside local dev URLs.

Daily alert rules can run from a Railway cron service or Function. Create a long random
`ALERT_SCHEDULER_TOKEN`, set the same value on the Flask service and the scheduled job, and point
`APP_URL` at the production app URL. Railway cron schedules use UTC.

Cron service fallback:

```bash
railway add --image oven/bun:1 --service daily-alert-digest
railway variable set --service daily-alert-digest APP_URL=https://your-railway-domain ALERT_SCHEDULER_TOKEN=...
```

Set the `daily-alert-digest` service start command to:

```bash
bun -e 'const res = await fetch(`${process.env.APP_URL}/api/alerts/scheduled/run`, { method: "POST", headers: { Authorization: `Bearer ${process.env.ALERT_SCHEDULER_TOKEN}`, "Content-Type": "application/json" }, body: "{}" }); const text = await res.text(); console.log(text); if (!res.ok) process.exit(1);'
```

Then set the service cron schedule, for example `0 11 * * *`.

Function path:

```bash
railway functions new \
  --path railway-functions/daily-alert-digest.ts \
  --name daily-alert-digest \
  --cron "0 11 * * *"
railway variable set APP_URL=https://your-railway-domain ALERT_SCHEDULER_TOKEN=...
```

## Modal Deploy

Modal is the simplest public host for this Flask app because it can serve the app directly as a WSGI
web function. The endpoint label is `jawaun-underlying-terminal`, so the deployed URL uses Modal's
standard `<workspace>--jawaun-underlying-terminal.modal.run` shape.

```bash
python -m pip install -e ".[deploy]"
modal secret create underlying-analyzer-env --from-dotenv .env --force
modal deploy modal_app.py
```

## Supabase Research Library

Saved research uses Supabase Auth plus Postgres RLS. The browser receives only the public project URL
and anon key from `/api/config`; the service-role key stays server/local only.

```bash
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-public-anon-key
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
SUPABASE_DB_PASSWORD=your-db-password
ALERT_SCHEDULER_TOKEN=long-random-shared-secret
```

Apply migrations with the Supabase CLI:

```bash
supabase link --project-ref your-project-ref --password "$SUPABASE_DB_PASSWORD"
supabase db push --password "$SUPABASE_DB_PASSWORD"
```

For magic links, add local and deployed URLs to Supabase Auth redirect URLs, including
`http://127.0.0.1:5058/*` for local testing and the Modal URL for production.

Alert Monitor uses `alert_rules`, `alert_runs`, and `alert_deliveries` with owner-scoped RLS.
Browser users can save and manually run their own rules with the anon key, while the daily
scheduler uses the server-only service-role key behind `/api/alerts/scheduled/run`. Rules can also
deliver scheduled digests to HTTPS webhooks, with delivery history recorded per run. Webhook rules
can send a server-side test delivery from the Alert Monitor before waiting for the daily scheduler.

## Quality Checks

```bash
python -m ruff check .
python -m mypy app tests
python -m pytest
```
