# The Underlying Analyzer Reboot

A reboot of the old `tube` Python chart backend and `tufe` frontend as one repo:

- Flask API for chart generation and stock summaries
- Static frontend with the original retro terminal styling
- `yfinance` as the primary provider, updated to the current package line
- Nasdaq public historical fallback for daily US equity OHLCV when yfinance fails
- Public TradingView watchlist links for portfolio, chart batches, volatility, and stock briefs
- SEC EDGAR source packs for filings, XBRL company facts, and Vision memo citations
- JSON exports for generated ticker/watchlist data

## Data Provider Notes

`yfinance` still works for many people, but it is unofficial Yahoo Finance access. Recent
failures have centered on unauthorized cookie/crumb responses and undocumented throttling.
The reboot keeps yfinance for broad coverage, options, and metadata, but US equity chart
endpoints can fall back to Nasdaq historical JSON so the app can still render daily charts
when yfinance has a temporary issue.

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
  SEC_USER_AGENT=...
railway up
railway domain
```

For magic links, add the Railway URL to Supabase Auth redirect URLs alongside local dev URLs.

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
```

Apply migrations with the Supabase CLI:

```bash
supabase link --project-ref your-project-ref --password "$SUPABASE_DB_PASSWORD"
supabase db push --password "$SUPABASE_DB_PASSWORD"
```

For magic links, add local and deployed URLs to Supabase Auth redirect URLs, including
`http://127.0.0.1:5058/*` for local testing and the Modal URL for production.

## Quality Checks

```bash
python -m ruff check .
python -m mypy app tests
python -m pytest
```
