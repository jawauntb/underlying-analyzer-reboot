# vol-watch: IV rank, implied minus realized, and the fast-drop setup

`app/vol_watch.py` is a standalone worker (`python -m app.vol_watch`) that runs as its
own Railway service (`vol-watch`, start command `python -m app.vol_watch`, no healthcheck) and pushes to a
phone only when a signal crosses a threshold.

## What it computes

Every `VOL_WATCH_CHAIN_MINUTES` (15) during US market hours, per watchlist ticker:

| metric | definition |
|---|---|
| `iv30` | ATM implied vol (call/put averaged) interpolated in variance-time to 30 calendar days from the two nearest listed expiries |
| `rv20` | annualized close-to-close realized vol over the last 20 trading days |
| `spread` | `iv30 - rv20` in vol points |
| `iv_rank` | position of today's `iv30` inside the trailing-year range of stored daily samples; until `VOL_WATCH_MIN_RANK_SAMPLES` (60) exist it is a proxy, `iv30`'s percentile among the trailing 252 daily `rv20` values, flagged `iv_rank_basis = "rv_proxy"` |
| `skew` | 5%-OTM put IV minus 5%-OTM call IV on the nearer expiry |

Every `VOL_WATCH_TICK_MINUTES` (5): the fast-drop check. For each index name on the
watchlist (XSP → SPY, QQQ → QQQ) it measures the drawdown from the trailing-hour high
on 5-minute bars and ^VIX's rise from its trailing-hour low. Default trigger: down
0.75% and VIX up 8%. One push per `VOL_WATCH_DROP_COOLDOWN_MINUTES` (60) per proxy.

## When it pushes

Only on a band change since the last tick (the state is persisted, so a restart does
not re-fire). Bands: IV rank high (>= 50) / low (<= 20); spread rich (>= +8) /
cheap (<= -3); skew spike (>= 12). Returning to the middle band is silent. A fresh
deploy fires once for anything that is already rich or cheap.

## Delivery

Any subset of:

- `NTFY_TOPIC` (+ optional `NTFY_SERVER`, default `https://ntfy.sh`): install the ntfy
  app, subscribe to the topic, done. Fast-drop alerts are sent at high priority.
- `PUSHOVER_USER_KEY` + `PUSHOVER_APP_TOKEN`.
- `VOL_WATCH_WEBHOOK_URL`: JSON `{title, body, priority, payload}`.

## State

`VOL_WATCH_STATE_PATH` (default `/data/vol_watch_state.json` when `/data` exists). The
Railway service mounts a volume at `/data` so IV samples accumulate across deploys and
the rank graduates from the proxy to real IV rank after 60 trading days.

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
Thin ETF chains (CANE, SOYB, VIXM) carry stale quotes; the ATM pick prefers strikes with
open interest and two-sided quotes, but treat their IV as a prompt to look, not a fill.
