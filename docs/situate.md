# Situate

Situate is the single-name research engine that reforms Prism. Where Prism *forecasts*
(a recommendation, a fair value, a point target), Situate *situates*: it tells you what a
stock is exposed to, what the odds look like per horizon, what the options market is
pricing, and what the business is saying — and then states a **posture**, never a buy/sell
call and never a point price target.

Situate is additive. Prism (`app/prism`, `/api/prism`, the `ubermemo` tool alias) stays in
place and keeps working; Situate lives beside it in `app/situate` and reuses Prism's data
plumbing wholesale (Massive/FRED clients, the point-in-time cache and store, EDGAR, Exa,
Anthropic, the PDF renderer). The only new dependency is `scipy`.

## What it produces

For a ticker `T` on date `t`, across horizons `h ∈ {1, 2, 3, 6, 12, 18}` months, Situate
builds a `SituatePacket` (contract in `app/situate/contract.py`). Every top-level key is
always present; a section that could not be built is `null` with a sibling
`<section>_error` and a row in `meta.errors`, so a client can index the same keys on every
response.

| Section | SPEC | What it is |
| --- | --- | --- |
| `exposure` | 5.1 | EWMA-ridge betas on an ETF/macro basket + a Fama-French factor view, with bootstrap SE, R², idiosyncratic share, beta paths and 6/12m change |
| `state` | 5.2 | 2×2 volatility×trend cell for SPY and `T`, an optional HMM second opinion, and VIX/HY-OAS/curve context percentiles |
| `base_rates` | 5.3 | Per-horizon empirical forward-return distribution (unconditional, conditional on the current cell, shrunk, vol-managed) plus the industry ETF's own rates |
| `implied` | 5.4 | Option-implied risk-neutral density (Breeden–Litzenberger), ATM IV, 25Δ skew, P(±10/±20%), and width-vs-history — per-horizon `null` with a reason when the chain is too thin |
| `fundamentals` | 5.5 | Momentum, quality, value z-scores and an 8-quarter trajectory (revisions/PEAD are `null` — no consensus-estimate feed) |
| `text` | 5.6 | Filing-diff change scores with quoted new/removed risks, and dated news events with sentiment |
| `levels` | 5.8 | Auction value area + moving averages, and cheap/rich **zones** from the implied quantiles |
| `stack` | 5.7 | The cross-sectional model — published **only** when its walk-forward gates pass, else `null` with a reason (the odds then fall back to base rates + implied) |
| `odds` | — | The single forward-return distribution the memo reads, per horizon: `source`, `quantiles`, `p_up`, `base_rate_q50`, `shrink_w` |
| `scenarios` | 6.6 | bull / neutral / bear at 3/6/12m, each a state + the matching odds quantile + the top-two exposure drivers |
| `memo` | 6 | The posture memo (see below) |

Returns are decimal fractions (`0.034` is 3.4%). Dates are ISO-8601.

## The memo (SPEC §6)

The memo follows a fixed template: **Headline / What you're buying / State / Odds by
horizon / The business / Scenarios / Zones / What would prove this wrong (three
falsifiers) / Confidence and caveats / Appendix**. It leads with a **posture** —
`odds_favorable`, `balanced` or `odds_unfavorable` at a stated horizon, with a conviction
in `[0, 1]` — never "buy" or "sell", never a point price target. Every quantitative claim
cites its module and version by id (`[C3]`), and the memo always ends with the research-only
disclaimer. Language is hedged ("the data suggests").

With no `ANTHROPIC_API_KEY` the deterministic template stands on its own — the memo section
is never empty. With a key, a model may rewrite the prose from the same briefing, but the
posture, falsifiers, determinants, zones and citations are always the engine's own; a memo
whose citation ids do not resolve against the engine catalogue falls back to the template.

## Odds merge

`odds` is assembled per horizon, in order of preference:

1. the **stack** when its gates passed — its target is the forward *excess* return over the
   industry ETF, so its quantiles are lifted to a total-return basis by adding the industry
   ETF's shrunk base-rate median (a documented proxy, never a fabricated number);
2. otherwise the equal-weight blend of the **shrunk conditional base rate** and the
   **option-implied real-world quantiles** — the guaranteed ship state;
3. otherwise whichever of the two is present on its own.

`p_up` is derived by inverting the piecewise-linear quantile CDF at a zero return. A horizon
with no usable source is `null` with a stated reason.

## HTTP API

Mounted under `/api/situate` and the alias `/api/research`. See
[api.md § Situate](api.md#situate-research) for the full request/response contract.

| Route | Purpose |
| --- | --- |
| `POST /api/situate` | Build the packet (1–3 minutes cold; `force`, `include_memo` flags) |
| `GET /api/situate/<ticker>` | The latest stored packet (`?as_of=` for a specific build) |
| `GET /api/situate/<ticker>/summary` | Bounded agent projection |
| `GET /api/situate/<ticker>/export?format=md\|json\|pdf` | Download the memo |
| `POST /api/situate/<ticker>/chat` | Ask one question about a built packet |

Admission is bounded exactly like Prism: two concurrent builds per process and one in-flight
build per non-loopback client; saturation returns `503` (process) or `429` (client) with
`Retry-After: 30`. `400` covers a malformed ticker/`as_of`/body; `404` when nothing is
stored; `500` carries a build failure.

The same capabilities are agent/MCP tools `situate`, `situate_get`, `situate_chat` and
`situate_export`.

## Command line

No server needed — the whole engine runs in-process:

```bash
python -m app.situate.cli NVDA                          # build, print the memo markdown
python -m app.situate.cli NVDA --export md,json,pdf --out ./out
python -m app.situate.cli NVDA --as-of 2026-06-30 --export json
python -m app.situate.cli NVDA --stored --export md     # read the latest stored packet
python -m app.situate.cli NVDA --chat "what would prove the read wrong?"
python -m app.situate.cli NVDA --summary                # the bounded agent projection
python -m app.situate.cli NVDA --no-stack               # skip the cross-sectional stack (faster)
```

Clients are built from the environment (`MASSIVE_API_KEY`, `SEC_USER_AGENT`, `EXA_API_KEY`,
`ANTHROPIC_API_KEY` + `SITUATE_TEXT_MODEL`/`ANTHROPIC_TEXT_MODEL`, `FRED_API_KEY`,
`PRISM_CACHE_DIR`). A missing dependency degrades that section rather than failing the build.

## Point-in-time discipline

Every module is a pure function of `(ticker, t, config)`. The panel filters `as_of <= t`;
fundamentals key on **filing date**, not fiscal period end; no options snapshot or price
after `t` enters a result. The lookahead test recomputes a packet at `t` after masking data
after `t` and asserts the exposure betas, base rates and odds are identical.

## Storage

Situate packets persist through the same store class as Prism, but rooted under a `situate/`
sub-directory of `PRISM_CACHE_DIR`, so a Situate packet never overwrites a Prism one for the
same ticker and date. Per-module results are cached under `situate_<module>` keys. Both tiers
degrade to local JSON when Supabase is not configured.

## Non-goals

Situate deliberately does **not** build: calendar-month seasonality; Fourier/spectral cycles;
derivatives (velocity/accel/jerk) of correlations or betas; entropy filters; any
group/gauge/category-theoretic layer; point price targets or buy/sell language; or any number
using data after `t`.
