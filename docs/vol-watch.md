# vol-watch: IV rank, implied minus realized, and the fast-drop setup

`app/vol_watch.py` is a standalone worker (`python -m app.vol_watch`) that runs as its
own Railway service (`vol-watch`, start command `python -m app.vol_watch`, no healthcheck) and pushes to a
phone only when a signal crosses a threshold.

## What it computes

Every `VOL_WATCH_CHAIN_MINUTES` (15) during US market hours, per watchlist ticker:

| metric | definition |
|---|---|
| `iv30` | IV of the nearest-50-delta call and put (averaged) on the two expiries bracketing 30 calendar days, interpolated in variance-time |
| `rv20` | annualized close-to-close realized vol over the last 20 trading days |
| `spread` | `iv30 - rv20` in vol points |
| `iv_rank` | position of today's `iv30` inside the trailing `VOL_WATCH_RANK_SAMPLES` (252) stored daily samples; `rank n/a` until that many exist, and the rank signal never fires on n/a |
| `skew` | 25-delta put IV minus 25-delta call IV on the nearer expiry; `n/a` when either wing has no passing quote |
| `zero_dte` | XSP only: same-day ATM straddle mid as a percent of spot (session implied move) vs the move realized so far today, from 5-minute closes |

Every IV is solved from the quote mid with Black-Scholes on the parity-implied forward
(`F = K + (C - P)e^{rT}` at the strike where call and put mids are closest), so VIX options
price off the future and dividend names off their forward. A quote counts only when
`bid > 0`, `ask >= bid`, and `(ask - bid) / mid < VOL_WATCH_MAX_REL_SPREAD` (0.25). The
vendor's own IV column is never used.

Sanity gates, each of which skips the ticker for that pass and sends nothing: no ATM
quote passes the filter; `iv30 < VOL_WATCH_MIN_IV` (5); and for SPX, XSP and SPY,
`|iv30 - VIX| > VOL_WATCH_VIX_GATE_POINTS` (2). Note that VIX is a variance-swap strike
and normally sits 1 to 4 points above ATM vol, so the 2-point gate will trip on quiet
days; widen it with the variable if XSP goes silent.

Every `VOL_WATCH_TICK_MINUTES` (5): the fast-drop check. For each index name on the
watchlist (XSP → SPY, QQQ → QQQ) it measures the drawdown from the trailing-hour high
on 5-minute bars and ^VIX's rise from its trailing-hour low. Default trigger: down
0.75% and VIX up 8%. One push per `VOL_WATCH_DROP_COOLDOWN_MINUTES` (60) per proxy.

## When it pushes

Only on a band change since the last tick (the state is persisted, so a restart does
not re-fire), and at most once per ticker per signal per
`VOL_WATCH_ALERT_COOLDOWN_MINUTES` (30). Bands: IV rank high (>= 50) / low (<= 20);
spread rich (>= +8) / cheap (<= -3); skew spike (>= 12). Returning to the middle band
is silent. A fresh deploy fires once for anything that is already rich or cheap.

## Delivery

Any subset of:

- `NTFY_TOPIC` (+ optional `NTFY_SERVER`, default `https://ntfy.sh`): install the ntfy
  app, subscribe to the topic, done. Fast-drop alerts are sent at high priority.
- `PUSHOVER_USER_KEY` + `PUSHOVER_APP_TOKEN`.
- `VOL_WATCH_WEBHOOK_URL`: JSON `{title, body, priority, payload}`.

## State

`VOL_WATCH_STATE_PATH` (default `/data/vol_watch_state.json` when `/data` exists). The
Railway service mounts a volume at `/data` so IV samples accumulate across deploys; the
rank prints once 252 daily samples exist.

## Running it

```
python -m app.vol_watch --once        # one full pass, prints metrics and anything fired
python -m app.vol_watch --test-push   # one test notification to every configured channel
python -m app.vol_watch               # the loop (sleeps outside 9:30-16:00 ET, Mon-Fri)
```

`VOL_WATCH_TICKERS` overrides the watchlist (comma-separated). Thresholds are the
`VOL_WATCH_*` variables listed in `.env.example`.

## Data

yfinance only. Index names map to Yahoo symbols (`XSP` → `^XSP`, `VIX` → `^VIX`).
Thin ETF chains (SOYB, VIXM) usually have no quote that passes the filter and are
skipped silently; the log says so each pass.

## Trade watcher (fills, cancels, kernel, ledger)

`app/trade_watch.py` runs as a thread inside the same service once a Robinhood token is
set (`ROBINHOOD_TOKEN`, optionally `ROBINHOOD_REFRESH_TOKEN` + `ROBINHOOD_DEVICE_TOKEN`
so a 401 refreshes instead of stopping; `python scripts/robinhood_token.py` prints all
three after one login on your own machine). Read-only by construction: the HTTP client
has a `get` method and nothing else, and no code path can place, change or cancel an
order. The one POST is the OAuth token refresh.

Between 09:25 and 16:15 ET it polls option and equity orders every
`TRADE_WATCH_POLL_SECONDS` (30) and:

- pushes `FILL <side> <qty> <legs> @ <price> | fees <x>` per newly filled order, multi-leg
  orders as one push with every leg and the net debit or credit;
- pushes `CANCELED <order>` when an order is cancelled or rejected, including one that
  was placed and pulled between polls;
- keeps the day state: realized P&L net of fees, open positions, each opening order's
  max loss at entry (debit paid, or width × 100 × qty minus credit for a spread; a
  partial close keeps the share of legs still open), and
  `kernel = realized loss + sum of open max losses`;
- pushes at 50, 80 and 100 percent of `TRADE_WATCH_DAY_BUDGET` (500) and
  `DAY DOUBLED, done for the day` at realized ≥ +$500, each once per day;
- pushes an opening snapshot at 09:31 ET and a close summary at 16:05 ET;
- appends every fill and the close summary to `trade_ledger.csv` on the volume and to
  the Google Sheet (`GOOGLE_SHEET_ID`, columns `ts_et, order_id, legs, side, qty, price,
  net, fees, realized_day, kernel_used, note`). The sheet write needs
  `GOOGLE_SERVICE_ACCOUNT_JSON` (the service account's JSON on one line) and the sheet
  shared with that service account's email as an editor. Without it the CSV still fills.

The replay test in `tests/test_trade_watch.py` runs the real 2026-10-19 XSP tape through
the state machine (fixture under `tests/fixtures/`) and asserts realized −$6 and kernel
$156 with the three 766P contracts open.

Fees: Robinhood's order objects carry no fee field, so `fees` is 0 unless the API adds
one (`regulatory_fees`, `fees` or `contract_fees` are read when present). Index option
fees are real money; the close summary says fees 0.00 until that changes.
