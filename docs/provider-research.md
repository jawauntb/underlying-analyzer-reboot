# Provider Research

Checked on 2026-06-08.

## Recommendation

Use updated `yfinance` as the primary provider and add a no-key Nasdaq fallback for daily
US equity OHLCV. That gives the app the same broad behavior it had before, while avoiding
a total outage when Yahoo changes a cookie, crumb, or rate-limit path.

For a public production deployment with heavy usage, add a keyed provider rather than
depending solely on Yahoo. Financial Modeling Prep is the best free keyed candidate for
this app because its free plan includes 250 calls/day and EOD historical data. Twelve Data
has a larger free daily allowance, but its credits/minute model and market limitations make
it less straightforward for multi-chart burst usage.

## Findings

- `yfinance` is still maintained. PyPI listed `1.4.1` on 2026-05-28.
- The package itself says it is not affiliated with Yahoo and is intended for research and
  educational use.
- A 2025 yfinance issue documents `401 Unauthorized` and `Invalid Cookie` failures around
  `quoteSummary`, which matches the class of old-app breakage.
- Alpha Vantage still has a free plan, but its official support page says the free stock API
  service is up to 25 requests/day.
- Financial Modeling Prep's pricing page says the Basic plan includes 250 calls/day, EOD
  historical data, and profile/reference data.
- Twelve Data's pricing page says the free Basic plan includes 8 API credits per minute and
  800/day.
- Marketstack's free plan is too small for this app: 100 requests/month and one year of EOD
  history.

## Implementation Choice

The app needs daily historical prices for most visualizations, plus optional richer data for
summaries, options, and metadata. Daily US equity OHLCV is the part that can be made
resilient without asking for an API key. The reboot therefore:

- calls `yf.download(..., auto_adjust=False, progress=False)` first;
- normalizes yfinance MultiIndex output;
- falls back to Nasdaq historical JSON for US equity tickers;
- keeps provider/source metadata in every API response;
- isolates all provider calls in `app/market_data.py`.
