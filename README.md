# The Underlying Analyzer Reboot

A reboot of the old `tube` Python chart backend and `tufe` frontend as one repo:

- Flask API for chart generation and stock summaries
- Static frontend with the original retro terminal styling
- `yfinance` as the primary provider, updated to the current package line
- Nasdaq public historical fallback for daily US equity OHLCV when yfinance fails
- Public TradingView watchlist links for portfolio, chart batches, volatility, and stock briefs
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

## Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m flask --app app.main run --port 5050
```

Open `http://127.0.0.1:5050`.

## Watchlist Workflow

Paste a public TradingView watchlist URL, set `Max results`, and generate any mode that accepts
tickers. Portfolio and Volatility combine the resolved symbols into one chart. Auction, Month Map,
Regression, and Brief generate one result per resolved ticker. The `Export JSON` button downloads
the structured result data, including resolved tickers, watchlist metadata, per-symbol metrics, and
any skipped-symbol errors.

## Quality Checks

```bash
python -m ruff check .
python -m mypy app tests
python -m pytest
```
