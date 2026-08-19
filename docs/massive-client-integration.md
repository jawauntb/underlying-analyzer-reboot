# Massive additive client integration

Copy/paste this into an upstream app that consumes the terminal API:

> The backend is now Massive-first and exposes these additive capabilities without changing existing chart, options-chain, error, or pagination contracts:
>
> - `GET /api/data/market/stream?ticker=AAPL&feed=trades|quotes|aggregates_minute|aggregates_second` for live stock events. Use it for live watchlists, quote headers, alerts, or charts; prefer it when the UI needs an actively updating price rather than a one-time snapshot. The transport is SSE backed by Massive WebSockets, and there is no yfinance fallback for event streams.
> - Add `asset_class=options` and a Massive option contract ticker such as `O:SPY...` to the same stream for live option trades, quotes, and aggregates. Use it only when the user is viewing a specific contract and the stream is explicitly enabled.
> - `GET /api/data/market/snapshot?ticker=AAPL` remains the stable one-shot quote/snapshot contract. Use it for screen load, pull-to-refresh, offline-safe caching, or clients that do not support streaming.
> - `GET /api/data/options/AAPL/chain?expiry=YYYY-MM-DD` returns the existing chain shape plus additive `*_implied_volatility`, `*_delta`, `*_gamma`, `*_theta`, `*_vega`, `*_bid`, `*_ask`, `*_volume`, and `*_contract` fields. Use it for options scanners, strike tables, IV/Greek displays, and liquidity checks; treat missing fields as unavailable.
> - `GET /api/data/options/AAPL/snapshot/<contract>` returns the additive single-contract snapshot. Use it when a user drills into one option and needs the most current quote/Greek snapshot.
> - `GET /api/data/market/news/AAPL`, `/api/data/market/ipos`, `/api/data/market/conditions`, `/api/data/market/snapshot/all`, and `/api/data/market/corporate-events` provide additive news, IPO, market-condition, full-market snapshot, and TMX event datasets. Integrate them only when the product has a visible use for that dataset; do not assume the dataset is entitled just because Stocks or Options is enabled.
> - `GET /api/providers` reports primary provider, fallback state, freshness hints, and stream readiness. `GET /api/capabilities` reports capability/freshness hints. Use these to gate UI, label freshness, and avoid showing a misleading realtime badge.
>
> Integration criteria: use streaming for continuously changing views, snapshots for initial load and refresh, options chain/snapshot for contract-level decisions, and events/news only for contextual research surfaces. Preserve the existing response envelopes and use the `provider`, `provider_note`, and capability/freshness fields for provenance. A 501 means the configured subscription does not expose that dataset; a 429/5xx/timeout may use the explicit backend fallback for compatible REST requests. Never infer realtime access from the provider name alone.

The native mobile client uses these rules in Ticker Lens: provider status is visible on load, and the richer options pulse is fetched only when the user explicitly opens Diagnose.
