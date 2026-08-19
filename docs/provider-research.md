# Provider Research

Checked on 2026-08-19. The Massive adapter is implemented in `app/massive.py` and routed
through `app.market_data.MarketDataClient`; this document records verified coverage and gaps.

## Recommendation

Massive is the first keyed adapter for capabilities it covers, with the current yfinance
implementation as the first fallback, and Nasdaq’s no-key daily US-equity path as the last
fallback. The route and chart builders consume a canonical internal model rather than
provider-specific JSON.

This gives the app a durable primary source without making a Massive subscription or temporary
upstream failure a hard dependency for existing chart routes. The routing flag is explicit:

| Condition | Route |
| --- | --- |
| No `MASSIVE_API_KEY` | yfinance → Nasdaq for daily US equities |
| Massive success with sufficient entitlement | Massive |
| Massive timeout, 429, 5xx, empty result, or 401/403 entitlement miss while `MARKET_DATA_FALLBACK_ENABLED=true` | yfinance → Nasdaq |
| Invalid request or unsupported symbol | normalized error, with coverage fallback allowed only when it can produce a valid result |

`MARKET_DATA_FALLBACK_ENABLED=false` is useful for staging and entitlement audits: it makes a
Massive failure visible instead of silently changing the data source. Production responses must
still identify the provider and freshness actually used.

## Official Massive findings

Massive’s [REST quickstart](https://massive.com/docs/rest/quickstart) documents API-key
authentication through either the query string or an Authorization header and describes the
common `status`, `results`, and `request_id` response pattern. The adapter uses the documented
query-key form and keeps the key server-side.

The [stock REST overview](https://massive.com/docs/rest/stocks/overview) exposes the core
families needed by this app: aggregates, snapshots, trades and quotes, reference data,
corporate actions, and technical indicators. The current implementation covers daily aggregate
bars, snapshots, ticker reference/search data, options chains/contracts/expirations, trades,
quotes, ticker events, dividends, splits, selected financial statements/ratios, and market
status. Technical indicators, IPOs, WebSockets, flat files, and partner datasets remain
documented gaps.

### Stock plan and freshness matrix

Access is endpoint-specific. The following examples are taken from the official endpoint pages
and are routing constraints, not assumptions about every Stocks endpoint:

| Stock capability | Basic | Starter | Developer | Advanced |
| --- | --- | --- | --- | --- |
| Custom OHLC bars | EOD / 2 years | 15-minute delayed / 5 years | 15-minute delayed / 10 years | Real-time / all history |
| Tick-level trades | Not included | Not included | 15-minute delayed / 10 years | Real-time / all history |
| Single-ticker snapshot | Not included | 15-minute delayed | 15-minute delayed | Real-time |
| Ticker events | Updated daily / 2 years | Updated daily / all history | Updated daily / all history | Updated daily / all history |

Sources: [custom stock bars](https://massive.com/docs/rest/stocks/aggregates/custom-bars),
[stock trades](https://massive.com/docs/rest/stocks/trades-quotes/trades), [single-ticker
snapshot](https://massive.com/docs/rest/stocks/snapshots/single-ticker-snapshot), and [ticker
events](https://massive.com/docs/rest/stocks/corporate-actions/ticker-events). The snapshot
page also notes that snapshot data is cleared at 3:30 AM ET and can begin repopulating as early
as 4:00 AM ET; consumers should use the returned timestamps, not wall-clock assumptions.

The adapter must preserve the distinction between `end_of_day`, `delayed_15m`, `realtime`, and
`updated_daily`. Current public responses expose `provider`, `provider_note`, and the
`/api/capabilities` freshness hints. They do not yet emit a universal `data_meta` object, so
clients must not infer freshness from the provider name alone or treat a plan hint as an
observation timestamp.

### Options are a separate entitlement surface

Massive documents [Options REST coverage](https://massive.com/docs/rest/options/overview) as a
separate Options Basic, Starter, Developer, and Advanced plan family. All Options plans include
contract reference endpoints; aggregate bars, daily summaries, option snapshots, trades, and
quotes are available only on select Options plans. The adapter must therefore model stock and
options entitlements independently and return `capability_unavailable` when a plan does not
include the requested options dataset. It must not substitute stock-plan access for options
access or present an empty chain as “no open interest.”

Stock dividends and splits are documented as included in all Stocks plans. Financial statements
and ratios are a separate, select-plan financials expansion surface, so the adapter exposes them
only through an explicit `MASSIVE_FINANCIALS_PLAN` declaration and still treats a live 401/403 as
the authoritative entitlement result.

### Events and partner data are not interchangeable

The native [Ticker Events](https://massive.com/docs/rest/stocks/corporate-actions/ticker-events)
endpoint is explicitly experimental, updated daily, and currently supports `ticker_change`.
It is suitable for identity continuity, not a general earnings or corporate-events calendar.

The [Partners API](https://massive.com/docs/rest/partners/overview) contains premium third-party
datasets with dedicated subscription requirements. For example, [TMX Corporate Events](https://massive.com/docs/rest/partners/tmx/corporate-events)
and Benzinga endpoints are not implied by a core Stocks or Options plan. Partner adapters must
be opt-in, capability-gated, and clearly labeled with their partner dataset; core-market
fallbacks cannot claim semantic equivalence.

## Adapter and contract choice

The adapters expose capability-level methods such as `get_history`, `get_aggregates`,
`get_snapshot`, `get_option_chain`, and `get_events`. Each method returns a typed internal
result. Raw additive routes currently wrap provider-shaped payloads as follows; normalized chart
and moneyline routes retain their existing response schemas:

```json
{
  "ticker": "AAPL",
  "asset_class": "stocks",
  "bars": [{"date": "2026-08-18", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
  "provider": "massive",
  "provider_note": "Massive market data; freshness depends on the configured Stocks and Options plans",
  "data": {"results": []}
}
```

`data_meta` remains a documented future additive field for source timestamps and routing
provenance. Until it is emitted, `/api/capabilities` and provider notes are the only freshness
signals exposed by this migration.

The baseline audit found no ThetaData imports, dependencies, endpoints, or consumers. The
remaining yfinance usage in `app/sec.py` is a separate Yahoo-hosted SEC filings mirror fallback;
it is not used for quotes, bars, options, contracts, or events.

The existing `/api/charts/...`, `/api/data/...`, analysis, MCP, and agent surfaces remain
stable. Provider metadata is additive, and a mixed watchlist may report `provider: "mixed"`
plus per-result metadata. See [SYSTEM_DESIGN.md](../SYSTEM_DESIGN.md) for the full boundary and
[chart-data-rendering.md](chart-data-rendering.md) for client rendering rules.
