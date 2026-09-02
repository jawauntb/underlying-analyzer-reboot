# Prism (working alias `ubermemo`)

A prism splits one beam into its spectrum. **Prism** splits one ticker's price into its
macro, factor, regime, spectral, entropy, fundamental and filing components, keeps every
intermediate number with its provenance, and recombines them into bull / neutral / bear
scenarios, a recommendation with entry and exit levels, and a memo you can chat with.

- Package: `app/prism/`
- HTTP: `POST /api/prism`, `GET /api/prism/<ticker>`, `GET /api/prism/<ticker>/summary`,
  `GET /api/prism/<ticker>/export`, `POST /api/prism/chat`
- Alias prefix: every route is also mounted at `/api/ubermemo…`
- Agent / MCP tools: `prism_memo` (alias `ubermemo`), `prism_get`, `prism_chat`, `prism_export`
- Research only. The memo says so, and no route can place an order.

---

## The packet

`build_prism_packet` returns one JSON object. **Every top-level key is always present.** A
section that could not be built is `null` with a sibling `<section>_error` string and a row
in `meta.errors`, so a client can index the same keys on every response and show
"unavailable: reason" instead of crashing.

| Key | What it holds |
| --- | --- |
| `ticker`, `as_of`, `generated_at`, `engine_version`, `name` | Identity of the build |
| `profile` | Name, sector, industry, market cap, description, listing date, related ETFs |
| `universe` | Every benchmark resolved for this ticker, with coverage and provider |
| `seasonality` | This calendar month over 1/2/5/10-year windows plus forward 1–18m distributions, for the ticker and its benchmarks |
| `macro` | FRED yields, curve shape, VIX, HY spread, broad dollar, WTI/Brent, FX, payrolls; gold and bitcoin from Massive |
| `relational` | Gauge-fixed beta/correlation per window, kinematics, cosine similarity, covariance, impact weights |
| `factors` | Fama-French 5 + momentum regression per 1/3/5/10-year window, residual cum-returns |
| `regimes` | 3-state Gaussian HMM on SPY (daily return, 10-day volatility MSE), transition matrix, current posterior, ticker behaviour per regime |
| `entropy` | Shannon entropy of rolling return distributions per window on a fixed-width ±3σ grid, weekly series, win-rate backtest |
| `spectral` | Top Fourier modes of detrended log price, cycle position, forward projection, consistency check |
| `eigen` | PCA of the monthly signal matrix, signal ranking, regime symmetry breaks, load-bearing test |
| `fundamentals` | Eight quarters joined across income/balance/cash-flow, derived ratios, growth, 4Q averages, forecast, stage |
| `filings` | Last ≤2 10-K and ≤3 10-Q with Business / Risk Factors / MD&A, per-filing summaries, cross-filing synthesis |
| `volatility` | Realized volatility per window with percentile, vol-of-vol, per-regime averages, implied ATM/skew/smile |
| `levels` | Auction value area, regression channel, Torque stage, Ridge state, 52-week extremes, ranked key levels |
| `news` | Categorised Exa + Massive items with a full `query_log` |
| `scenarios` | Weighted **log-return** mixture of the component forecasts (each shrunk toward the market prior and clamped), three cases with per-horizon percentiles, entry band, timing, watch signals |
| `recent` | Last 20 and 60 sessions: return, relative to SPY and sector, volatility, entropy, regime |
| `memo` | Recommendation, entry/exit/reassess, markdown memo, key determinants, what is priced in, citations |
| `sources` | Provenance rows: provider, symbol or series id, url, `fetched_at` |
| `meta` | `errors`, `source_status`, `timings_ms`, `cache`, `unavailable`, `stored` |

Percent returns are decimal fractions (`0.034` is 3.4%). Dates are ISO strings. A number
that could not be computed is `null` with a stated reason — never a placeholder.

---

## Calibration

Left alone, every quantitative component in the packet is a trend extrapolation, and they
compound. A regime block priced with NVDA's own bull-regime daily mean (+0.28%/day) reads as
+100% a year; a spectral fit that extrapolates a ten-year log-price trend plus a large 839-day
cycle reported a +198% twelve-month projection. Three mechanisms, all recorded in the packet,
keep the published numbers inside what the evidence supports.

### 1. Shrinkage toward a market prior

Before the mixture sees them, every component's expected return at every horizon is shrunk
toward the market's own long-run drift:

```
prior(h)              = expm1(mean(diff(log SPY)) * h)          # scenarios.prior
confidence(c, h)      = clip(evidence(c,h) * skill(c) * horizon_factor(h), 0, 0.90)
shrink_weight(c, h)   = 1 - confidence(c, h)
expected_return(c, h) = confidence * raw + shrink_weight * prior(h)
```

The prior comes from the SPY series the packet already loaded (`scenarios.prior.source`); if no
market series is available a documented 8% annual assumption stands in and says so. The three
confidence terms are each a count or a measured score:

| Term | What it is |
| --- | --- |
| `evidence(c, h)` | The component's own published `confidence` — seasonality's `min(n_years/20, 1)`, the HMM's posterior switch confidence, spectral's `R² × horizon damping × hold-out consistency`. Components publishing `confidence_by_horizon` are read per horizon. |
| `skill(c)` | The walk-forward out-of-sample R², mapped by `clip(0.5 + 0.5*clip(skill/0.05, -1, 1), 0.25, 1)`. A component the backtest could not score gets a neutral `0.5`. |
| `horizon_factor(h)` | `blocks / (blocks + 10)` where `blocks = n_observations / h` — the number of *non-overlapping* horizon-length blocks actually behind the claim. Ten years of daily closes gives ≈0.92 at one month, ≈0.50 at twelve. |

Each component carries a `shrinkage` block with `raw_expected_return`, `prior`, `shrink_weight`,
`confidence`, `skill_factor`, `horizon_factor`, `clamp_bounds`, `clamped` and the final
`expected_return`, so how far the calibration moved a component — and why — is always visible.

### 2. The plausibility clamp

The shrunk value is then clipped to the ticker's own empirical `[p5, p95]` of historical rolling
`h`-day returns, measured on the loaded close series and published as `scenarios.clamp_bounds`.
Nothing forecasts a move the name has never made. It is a backstop rather than the mechanism:
for a high-beta name the band is wide and the shrinkage above is what binds.

### 3. Spectral projection discipline

`spectral.projection` no longer extrapolates the whole-sample OLS slope, which is set by the
sample's largest re-rating. It uses a **robust recent trend** — the median one-day log change
over the last ~2 years, shrunk 50% toward zero (`spectral.robust_trend`) — damps the cycle term
by `reconstruction_r2`, and **truncates the cycle extrapolation at a quarter of the dominant
period** (`cycle_extrapolation_limit_days`). Past that point the horizon's `confidence` is forced
below 0.3 and keeps falling, so the mixture shrinks the component away. `spectral.trend` still
reports the OLS fit that defines the residual the modes are estimated on.

### 4. The mixture lives in log-return space

A Gaussian on the *simple* return has no floor, and a high-volatility name walked
straight through it: MU (80% annualised realised volatility, 12-month mixture σ of 1.51)
printed a twelve-month bear case of **−176%** and an eighteen-month one of −210%.

Each component's shrunk `(mean, sigma)` is therefore moment-matched onto a lognormal
(`scenarios.to_log_space`):

```
s² = log(1 + (sigma / (1 + mu))²)
m  = log1p(mu) - s² / 2
```

which reproduces `E[X] = mu` and `sd[X] = sigma` **exactly**, so the change of space alters
the distribution's shape and nothing else. `s` is derived rather than assumed to be the
published `sigma` because the components disagree about which space their spread was
measured in — the volatility fallback is a log-return sd, seasonality's comes from p10/p90
of simple forward returns, the regime block's from per-regime daily simple-return variance —
and reading every `sigma` as already-log would silently inflate the ones that are not.

The mixture moments, the `± 0.5σ` case cuts, the case probabilities and the truncated
conditional means (`truncated_mixture_mean`, unchanged) are all computed on
`log(1 + return)`; every published figure is converted back with `expm1` for returns and
`exp` for prices. So a return is bounded below by −100%, every price percentile is strictly
positive and monotone, and `price_p50 = spot · exp(median_log)`. `scenarios.return_space`
and `mixture_parts_space` record it; `mixture_parts` triples are `[weight, mu_log, sigma_log]`.

`distribution[h]` keeps `mean` / `std` / `skew` / `kurtosis` in **simple-return** terms
(closed-form moments of the lognormal mixture, so `mean` and `std` are numerically identical
to what the old Gaussian mixture reported) and adds `mean_log`, `std_log`, `skew_log`,
`kurtosis_log`, `geometric_mean_return` and `cut_*_log`. Each case block adds
`expected_log_return`, `median_log_return` and `p10_log` / `p50_log` / `p90_log`.

One consequence is intended and visible: a lognormal mixture is right-skewed, so its median
sits below its mean by the variance drag. For a name whose 12-month mixture σ is 150% that
gap is large, and the neutral case and the fair value move down accordingly. That is what
those two moments have always implied; the old symmetric mixture simply hid it.

### 5. Entropy on a fixed-width grid

`entropy` bins on **`bin_grid: "fixed_width_3sigma"`**: `bins` equal-width cells spanning
`[-3σ, +3σ]` where σ is the *full-sample* daily-return standard deviation
(`sigma_full_sample`), with returns outside the range clipped into the edge bins and `H`
normalised by `log2(bins)`.

The grid is fixed over calendar time, so two windows are measured on the same ruler and the
0.35 / 0.70 `structure` / `mixed` / `noise` thresholds can actually fire. The previous full-sample
**quantile** grid could not: any window resembling the full sample puts a tenth of its mass in
each of the ten cells and scores `H ≈ 1`, so every window of a liquid equity read "noise"
(NVDA: 0.88–0.99 across all five windows). That reading is retained per window as `H_quantile`.

Read the label as dispersion, not predictability: *structure* means the window's returns
concentrate in a few cells of the ticker's long-run range, *noise* means they fill it.
`percentile` and `relative_classification` place the same reading inside the ticker's own history
of that window length. σ is estimated in-sample, which the backtest states in `bin_grid_note`
rather than hiding.

---

## Workstream layout

| Module | Owner | What it does |
| --- | --- | --- |
| `contract.py` | W1 | `empty_packet()`, `PACKET_KEYS`, `set_section`, `validate_packet` |
| `universe.py`, `data.py`, `cache.py`, `macro.py`, `seasonality.py`, `relational.py` | W1 | Benchmarks, Massive history, two-tier cache, FRED, calendar-month statistics, gauge-fixed cross-asset math |
| `factors.py`, `hmm.py`, `regimes.py`, `entropy.py`, `spectral.py`, `eigen.py`, `scenarios.py` | W2 | Factor regressions, pure-numpy Gaussian HMM, entropy, Fourier cycles, PCA, the scenario mixture |
| `fundamentals.py`, `filings.py`, `news.py`, `volatility.py`, `levels.py`, `memo.py`, `chat.py`, `store.py`, `export.py`, `engine.py`, `routes.py` | W3 | Narrative sections, orchestration, persistence, exports, HTTP |

---

## The W3 sections in detail

### `fundamentals.py`

Massive's three statement endpoints (`income-statements`, `balance-sheets`,
`cash-flow-statements`) are joined on `period_end` into eight quarterly rows, newest first.

Two provider quirks are handled explicitly and verified live on 2026-09-01:

- The statement endpoints ignore `limit` and `sort`, returning the whole history ascending.
  Prism sorts and slices in process.
- The `ratios` endpoint **ignores the `tickers` filter** the statement wrapper sends and
  returns 2,000 rows for the whole market. Prism passes `ticker` (singular) instead, which
  returns exactly one row.

Derived ratios (P/E, P/S, P/B, EV/EBITDA, EV/EBIT, EV/Sales, D/E, FCF yield, NAV/share) are
computed from the statements with a trailing-twelve-month window; the vendor snapshot fills
only the gaps (dividend yield, returns on capital, liquidity ratios). `ratios_source` names
which is which per key. Growth refuses a percentage off a non-positive base — a swing from a
loss to a profit has no meaningful percentage and inventing one is exactly the number a memo
should never carry. The forecast is a linear revenue trend times a per-fiscal-quarter
seasonal factor and is refused below six quarters of revenue. If Massive has no statements
at all, the section falls back to SEC XBRL through `app.sec_trend.build_sec_trend_pack` and
says so in `provider`.

### `filings.py`

`app.sec.latest_filings` stops at the most recent 10-K and 10-Q and trims each section to
1,800 characters. Prism needs the last two annuals and three quarterlies at roughly 12,000
characters a section, so this module walks the submissions index itself while reusing
`sec.filing_url`, `sec.normalize_document_text` and `sec.SECTION_SPECS` for the parsing.
A 10-Q also numbers its items differently: MD&A is Part I Item 2 rather than Item 7, and
the risk factors sit in Part II Item 1A followed by "Unregistered Sales" rather than
"Properties". Running the annual patterns over a quarterly report finds the
table-of-contents line, fails to find a terminator, and returns the financial statements
instead — so quarterly reports get their own spec set. They also need a different
selection rule: `app.sec.extract_between` keeps the longest candidate, which is right for a
10-K but wrong for a 10-Q, where an earlier cross-reference ("see Item 1A. Risk Factors
for…") swallows the real Part II section and is therefore longer. Quarterly sections take
the last occurrence that is actually followed by the next item.

Documents are fetched three at a time behind `SecClient`'s own rate gate. Each filing gets a
bounded model summary; the cross-filing synthesis asks for strict JSON with six keys
(performance, risks, growth opportunities, new business lines, operating context,
capex/suppliers/customers) and falls back to filing excerpts when the model is absent or the
reply will not parse.

### `news.py`

Six curated Exa searches — company, industry, regulation, policy, forex, macro — over a
45-day window, plus Massive `/v2/reference/news` as an independent second source. Items are
deduplicated on the URL path and sorted newest first. `query_log` records every query, its
result count and its error, so a thin section reads as "the search returned nothing" rather
than "the engine did not look".

Massive paginates the whole feed for a ticker — 400 rows for a liquid name, most of them
market round-ups tagged with a dozen symbols. Prism keeps the newest 15 rows that carry the
ticker among at most six, which is the difference between "stories about this company" and
"stories that mention it"; the filter and the raw row count are both in the query log.

### `volatility.py`

Realized volatility per 1m/3m/6m/1y window with its rolling average and its percentile
against the full available history, plus vol-of-vol (the annualised volatility of the 21-day
realized-vol series) and per-regime averages when a regime label series is supplied.

Implied volatility comes from the Massive option-chain snapshot at the **nearest standard
monthly expiry** (the third Friday at least five days out) rather than the front weekly,
whose implied volatility is dominated by the next few sessions. The chain wrapper returns
the nine strikes nearest the money, which is enough for an ATM reading and a usable smile
but not always enough to reach a true 25-delta wing — so `skew_25d` is `null` with a stated
reason and the actual deltas when the nearest available wings are further than 0.10 from
0.25. `variance_risk_premium` is ATM implied minus trailing one-month realized.

### `levels.py`

Reuses `app.chart_data`'s builders (`build_auction_chart_data`,
`build_regression_chart_data`, `build_torque_chart_data`, `build_ridge_growth_chart_data`)
and keeps only the numbers. `key_levels` merges the value area, regression channel, moving
averages, 52-week extremes and swing range into one list ranked by distance from the last
price. Each family is guarded independently: a Torque failure (it needs SEC data) does not
cost you the value area.

### `memo.py`

`project_packet(packet)` renders the whole packet as a bounded markdown briefing (default
25,000 characters) with one line per fact, a section naming everything that failed, and a
numbered citation list. `derive_recommendation(packet)` derives the call mechanically from
the scenario mixture:

```
edge  = P(bull) - P(bear)            at the reference horizon
value = fair_value / current - 1     clipped to +/- 0.50
score = edge + value

score >  0.35  strong_buy
score >  0.12  buy
score > -0.12  hold
score > -0.35  sell
otherwise      strong_sell

conviction = min(1, |score| / 0.6), cut 30% when the 3-month return
             distribution is classified as noise, raised 15% when it is
             structure, then scaled by how many sections actually built
strength   = strong >= 0.66, normal >= 0.33, weak below
```

The model is given that baseline and asked for a two-block reply: the fields as JSON
between `<PRISM_JSON>` markers, then the memo as markdown between `<PRISM_MEMO>` markers.
Keeping a two-thousand-word document out of a JSON string matters — escaped, one stray
quote makes the whole reply unparseable, and a reply cut at the token limit loses
everything. With the memo in its own block a truncated reply still yields the fields and
whatever prose arrived, flagged as `truncated: true`. A single JSON object is still
accepted as a fallback.

Anything outside the recommendation grammar is replaced by the derived value. Citation ids
are checked against the briefing's list: `citation_ids_used` holds the ones that exist,
and `unknown_citation_ids` names anything the model invented (a `[C_regime]`-style label,
or an id past the end of the list) rather than letting it read like a checked reference.

With no API key at all, the deterministic memo *is* the memo: a complete markdown document
with the same recommendation, targets, signal-versus-noise section, priced-in section and
citations, marked `method: "deterministic"`.

### `chat.py`, `store.py`, `export.py`

`chat_turn` answers from the same projection the memo was written from, so a question about
any number is answered against the same evidence. Turns persist to `prism_chats` (Supabase)
or a local JSON thread, and a client can hold nothing but a `conversation_id`.

`store.py` mirrors `cache.py`: a local JSON tier under `PRISM_CACHE_DIR/packets/<TICKER>/<as_of>.json`
that always works, plus an optional Supabase tier (`prism_packets`, `prism_chats`) when
`SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are set. A Supabase failure lands in the
returned record's `errors`; it never loses the local copy and never raises.

`export.py` produces `json` (the raw packet), `txt` (the memo followed by every section as
fixed-width tables) and `pdf` (the typeset memo through `app.memo_pdf`).

### `engine.py`

```python
build_prism_packet(
    client, ticker, *, sec_client=None, exa_client=None, text_generator=None,
    as_of=None, include_memo=True, force=False, cache=None, store=None,
    fred_client=None, years=10, api_key=None, text_model=None,
    persist=True, max_workers=4,
) -> dict
get_prism_packet(ticker, as_of=None, *, store=None) -> dict | None
prism_summary(packet, *, max_news=5) -> dict
```

Order of operations:

1. `profile` (serial — the universe depends on the sector).
2. Seven independent fetches concurrently on a four-worker pool: universe history, macro,
   fundamentals, filings, news, the 1-year OHLCV history for levels, and the SEC trend pack.
3. W1 sections: seasonality, relational.
4. W2 sections: factors, regimes (which also decodes a daily regime-label series), entropy,
   spectral.
5. W3 sections: fundamentals, filings, news are placed from the fan-out; volatility and
   levels are computed.
6. `scenarios`, then `eigen` (whose load-bearing test intervenes on the scenario weighting),
   then `recent`.
7. `memo`, then persistence.

Every step runs inside `_guard`, which records `meta.timings_ms` and turns any exception into
`section = None` plus `<section>_error` plus a `meta.errors` row. W1 and W2 modules are
imported lazily inside those guards, so a checkout missing one of them degrades section by
section instead of failing to import.

Without `force`, a packet already stored for the same `as_of` is returned as-is with
`meta.cache.packet = "hit"`.

The eigen load-bearing test is a real intervention, not a formula sensitivity: for each
monthly signal, `signal_prediction_history` reconstructs what that signal *would have*
forecast for next month using only months whose forward window had already settled, and
`scenarios.make_weight_fn` re-runs the walk-forward weighting with that signal removed. The
reported `weight_delta_if_removed` is the L1 distance between the two weight vectors.

---

## HTTP contract

All bodies are `application/json`. Errors are `{"error": "..."}`. Every path below also
exists under `/api/ubermemo`.

### `POST /api/prism`

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `ticker` | string | — | Required. Max 16 chars, `A-Z0-9.-:^` |
| `force` | boolean | `false` | Rebuild even if today's packet is stored |
| `include_memo` | boolean | `true` | `false` skips the model call and returns data only |
| `as_of` | string | today | ISO date to build against |

```bash
curl -s -X POST http://127.0.0.1:5050/api/prism \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"NVDA","force":true}'
```

- `200` — the full packet.
- `400` — missing/malformed ticker, or a body that is not a JSON object.
- `429` — this client already has a build in flight (`Retry-After: 30`).
- `503` — the process is at capacity, two concurrent builds (`Retry-After: 30`).
- `500` — the build raised; the message carries the reason.

A cold build fans out to Massive, FRED, SEC EDGAR, Exa and Anthropic and takes **one to
three minutes**. Proxies should allow at least 180s and poll `GET /api/prism/<ticker>` while
waiting.

### `GET /api/prism/<ticker>`

Query: `as_of` (optional ISO date). `200` with the stored packet, or `404` when none exists.

### `GET /api/prism/<ticker>/summary`

The bounded agent projection: recommendation, targets, key determinants, priced-in,
scenario cases and weights, regime, entropy, this-month seasonality, fundamentals stage and
ratios, volatility, five news items, a 1,500-character memo excerpt, the list of sections
that are unavailable, and `meta.errors`. Typically under 15 KB. `404` when nothing is
stored.

### `GET /api/prism/<ticker>/export?format=txt|json|pdf`

`200` with the bytes, `Content-Disposition: attachment; filename="prism-<TICKER>-<as_of>.<ext>"`,
and a `Content-Length`. `400` for an unknown format, `404` when nothing is stored.

| `format` | Content-Type |
| --- | --- |
| `txt` (default) | `text/plain; charset=utf-8` |
| `json` | `application/json` |
| `pdf` | `application/pdf` |

### `POST /api/prism/chat`

| Field | Type | Notes |
| --- | --- | --- |
| `ticker` | string | Required; a packet must already be stored |
| `message` | string | Required, max 4,000 characters |
| `history` | array | Optional `{role, content}` turns; last 40 are used |
| `conversation_id` | string | Optional; continues a stored thread |
| `as_of` | string | Optional ISO date of the packet to answer from |

Response:

```json
{
  "conversation_id": "…", "ticker": "NVDA", "reply": "…",
  "citations": [{"id": "C2", "claim": "…", "source": "…", "url": null}],
  "available_citations": [...], "model": "…", "method": "model|deterministic",
  "reason": null, "history": [...], "store_errors": [], "generated_at": "…"
}
```

`400` for a missing/oversized message or a non-list `history`; `404` when no packet is
stored for the ticker.

### `GET /api/prism/`

Engine metadata: name, alias, version, and the route map.

---

## Command line

`app/prism/cli.py` runs the whole engine in-process, so a memo can be produced
without standing up Flask. It is the local entry point the skill's `--local`
mode uses.

```bash
# Build today's packet and print the full text report
python -m app.prism.cli NVDA --format txt

# The same packet as JSON, or as a PDF written into a directory
python -m app.prism.cli NVDA --format json
python -m app.prism.cli NVDA --format pdf --out ./out     # prints the written path

# Read the latest stored packet instead of rebuilding it
python -m app.prism.cli NVDA --stored --format txt

# Rebuild from the sources, ignoring today's stored packet
python -m app.prism.cli NVDA --force

# Numbers only: skip the Anthropic memo pass (much cheaper)
python -m app.prism.cli NVDA --no-memo

# A specific as-of date (strict ISO; anything else is rejected)
python -m app.prism.cli NVDA --as-of 2026-06-30

# The bounded agent projection, and one chat turn about the packet
python -m app.prism.cli NVDA --summary
python -m app.prism.cli NVDA --chat "What breaks the thesis?" --conversation-id abc123
```

Secrets come from the environment (`MASSIVE_API_KEY`, `FRED_API_KEY`,
`ANTHROPIC_API_KEY`, `EXA_API_KEY`, `SEC_USER_AGENT`, optional `SUPABASE_*`); a
`.env` beside the repo root is loaded if present. `PRISM_CACHE_DIR` decides where
the series cache, the built packets (`<dir>/packets/<TICKER>/<as_of>.json`) and
the chat threads land, so point it at a scratch directory to keep a run isolated.

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `2` | Usage error (missing or empty ticker) |
| `3` | The engine module could not be imported |
| `4` | The build, export or chat failed |

Sections that could not be computed never fail the run: they are reported on
stderr as `note: <source>: <reason>` and the packet still prints.

---

## Configuration

| Variable | Purpose |
| --- | --- |
| `PRISM_CACHE_DIR` | Series cache, packets and chat threads (default `.prism-cache`, gitignored) |
| `PRISM_CACHE_ENABLED` | `0` disables the series cache |
| `PRISM_CACHE_TTL_DAYS` | Series cache lifetime (default 31) |
| `PRISM_STORE_ENABLED` | `0` disables packet/chat persistence |
| `PRISM_TEXT_MODEL` | Override the memo/chat model; falls back to `ANTHROPIC_TEXT_MODEL` |
| `MASSIVE_API_KEY` | Prices, financials, options, news |
| `FRED_API_KEY` | Yields, VIX, HY spread, dollar, oil, FX, payrolls |
| `SEC_USER_AGENT` | Required by EDGAR for the filings section |
| `EXA_API_KEY` | The news and policy searches |
| `ANTHROPIC_API_KEY` | The memo and chat (optional — both degrade deterministically) |
| `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` | Shared cache/packet/chat tiers |

Flask config keys `PRISM_MARKET_CLIENT`, `PRISM_SEC_CLIENT`, `PRISM_EXA_CLIENT`,
`PRISM_TEXT_GENERATOR` and `PRISM_STORE` override the shared clients; they exist so tests
can inject fakes without disturbing the rest of the terminal.

Tables come from `supabase/migrations/20260901120000_create_prism_tables.sql`
(`prism_series_cache`, `prism_packets`, `prism_chats`).

**Yahoo/yfinance is blocked from the engine's network.** `app.prism.data.build_prism_client`
builds a `MarketDataClient` with the fallback disabled on purpose, and nothing in Prism
depends on it. VIX comes from FRED `VIXCLS` because Massive returns 403 on `I:VIX`.

---

## Agent and MCP tools

| Tool | Method + path | Cost | Notes |
| --- | --- | --- | --- |
| `prism_memo` (alias `ubermemo`) | `POST /api/prism` | `llm` | The full build |
| `prism_get` | `GET /api/prism/{ticker}` | `fast` | Read what is stored |
| `prism_chat` | `POST /api/prism/chat` | `llm` | Question one packet |
| `prism_export` | `GET /api/prism/{ticker}/export` | `slow` | `txt` / `json` / `pdf` |

They are declared once in `app/tool_registry.py`; `/api/docs`, `/api/openapi`, both MCP
transports and the in-product agent are generated from that declaration. `prism_memo`
carries the alias `ubermemo`, and `get_tool("ubermemo")` resolves to it, so a caller that
learned the working name keeps working.

Adding four tools pushed the `list_capabilities` catalog past
`app.tool_executor.MAX_RESULT_CHARS` (14,000), which would have handed the model a
truncated index of the very tools it is choosing between. `tool_catalog_payload()` is now
explicitly an index: it keeps name, lane, summary, when-to-use, cost and route, and drops
the argument list and the response contract, both of which are published in full at
`GET /api/openapi` and in MCP `tools/list` (and are pointed at by the catalog's new
`schemas` block). Nothing that `/docs` or `/api/docs` renders was removed.

---

## Tests

```bash
. .venv/bin/activate
python -m pytest -q tests/test_prism_narrative.py tests/test_prism_routes.py
python -m pytest -q tests/test_prism_core.py tests/test_prism_quant.py
python -m ruff check . && python -m mypy app
```

`test_prism_narrative.py` covers the W3 modules and a full offline engine build with fake
market, SEC, Exa and text clients. `test_prism_routes.py` pins the HTTP contract, the
`/api/ubermemo` alias, the 429/503 admission behaviour and the tool-registry bindings.
Neither test touches the network.

A live pass (real keys, real providers) is:

```bash
set -a; . path/to/shared.env; . path/to/underlying-terminal.env; set +a
python -c "
from app.prism.data import build_prism_client
from app.prism.engine import build_prism_packet
from app.sec import SecClient
from app.exa import ExaClient
import os
packet = build_prism_packet(
    build_prism_client(), 'NVDA',
    sec_client=SecClient(user_agent=os.getenv('SEC_USER_AGENT')),
    exa_client=ExaClient(), api_key=os.getenv('ANTHROPIC_API_KEY'), force=True,
)
print(packet['memo']['recommendation'], packet['meta']['errors'])
"
```
