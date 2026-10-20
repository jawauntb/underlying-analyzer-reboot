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

`app/trade_watch.py` runs as a thread inside the same service once a Robinhood login is
configured. No computer is needed: set `ROBINHOOD_USERNAME` and `ROBINHOOD_PASSWORD` on
the vol-watch service in the Railway dashboard (works in a phone browser). On the next
start the server logs in with robin_stocks, pushes "approve robinhood login" to the
phone, and waits while Robinhood shows its own device-approval prompt in the Robinhood
app. After the approval the session is cached on the volume (`/data/.tokens`) and later
restarts reuse it without another prompt. If Robinhood asks for an SMS or email code
instead, the server cannot type it and pushes "robinhood login needs a code"; switch
two-factor to app approval and restart the service. `ROBINHOOD_TOKEN` (plus the refresh
and device tokens) still works if a token is ever available another way.

What "read-only" means here: Robinhood has no read-only login, so the session itself
could trade. The code that holds it cannot: the HTTP client has a `get` method and
nothing else, and no code path places, changes or cancels an order. The one POST is
the OAuth token refresh.

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
- appends every fill and the close summary to `trade_ledger.csv` on the volume (columns
  `ts_et, order_id, legs, side, qty, price, net, fees, realized_day, kernel_used, note`).
  The Google Sheet write is optional and off unless `GOOGLE_SERVICE_ACCOUNT_JSON` is
  set; a Claude chat with the Robinhood connector reads fills from Robinhood directly,
  so the ledger does not depend on it.

The replay test in `tests/test_trade_watch.py` runs the real 2026-10-19 XSP tape through
the state machine (fixture under `tests/fixtures/`) and asserts realized −$6 and kernel
$156 with the three 766P contracts open.

Fees: Robinhood's order objects carry no fee field, so `fees` is 0 unless the API adds
one (`regulatory_fees`, `fees` or `contract_fees` are read when present). Index option
fees are real money; the close summary says fees 0.00 until that changes.

## Futures watcher (ES, NQ, VX, GC, CL, GLD)

`app/futures_watch.py` runs as a thread in the same service on futures hours (Sunday
18:00 ET to Friday 17:00 ET, minus the 17:00 to 18:00 break), ticking every
`FUTURES_TICK_MINUTES` (5). Contracts: ES and NQ front and back quarterly (roll a week
before the third Friday), GC front and back active months (G J M Q V Z, roll when the
contract month is ten days out), CL front and back monthly (roll three days before the
20th of the prior month), GLD, and VIX futures front and back monthly from Cboe's
delayed combined quotes (`VX/V6` style, weeklies excluded) with VIX and VIX3M beside
them. The chosen symbols are written to the state file each day.

Pushes:

- `futures <slot> ET` snapshot at each `FUTURES_SNAPSHOT_TIMES` slot (default
  06:30, 08:30, 12:30, 16:30): last, change vs prior settle and trailing-hour move per
  front month, back month last, VIX futures spread with contango or backwardation, gold
  and crude. A slot the service slept through by more than 90 minutes is skipped rather
  than sent stale.
- `MOVE <contract> ±x% in 60m` when the front month moves more than its threshold
  inside the trailing hour, measured from the hour high (down) or hour low (up):
  `FUTURES_MOVE_ES` 0.75, `FUTURES_MOVE_NQ` 0.75, `FUTURES_MOVE_GC` 1.0,
  `FUTURES_MOVE_CL` 2.0, `FUTURES_MOVE_VX` 8.0, `FUTURES_MOVE_GLD` 1.0. One per contract
  per `FUTURES_COOLDOWN_MINUTES` (60). ES, NQ and VX moves go out at high priority.
- `OVERNIGHT ES ±x% vs settle` outside the cash session when ES is more than
  `FUTURES_OVERNIGHT_PCT` (1.0) from the prior settle, once per session day.
- `VIX TERM: backwardation` / `back to contango` when the front month crosses the back
  month, on the change only.

`python -m app.vol_watch --futures-once` prints one snapshot; add `--push` to send it.

## Trade cards (every alert in one template)

`app/trade_cards.py` renders every alert that implies a trade as one card:

```
ticker:
Open:
Close:
EV:
Timeframe:
Theta:
Hoping For:
Reason:
additional context:
```

The structure on the card is priced off the live chain through the same quote filter
as the vol watcher (bid > 0, relative spread under 25%, IV solved from the mid on the
parity forward): a 45-delta long for the gamma phase, a 25-delta credit spread with the
widest wing that keeps max loss under `CARD_MAX_LOSS` (300) for the premium-selling
cases, a 50/25-delta debit spread when options are cheap. EV follows the babe-ruth
formulas (credit spread: `credit × p(otm) − (width − credit) × p(itm)` with the short
delta as p(itm); long option: debit, breakeven, delta; debit spread: needs
`p(win) > debit / width`). Theta is Black-Scholes per day, or premium over hours left
for a same-day contract. When the chain is unavailable the card still goes out with
`Open: n/a (chain unavailable)` and the reason intact. Context carries VIX, the VX
front/back and curve shape, ES vs settle, and today's kernel when the trade watcher has
state.

Which card each alert gets:

| alert | card |
|---|---|
| ES below a level, VX front up under 8% in the hour | buy 45-delta XSP 0DTE put; convert to a zero-cost spread 20 ES points lower |
| ES below a level, VX front already up 8%+ | sell 25-delta XSP 0DTE put credit spread |
| ES above a level | sell 25-delta XSP 0DTE call credit spread |
| FAST DROP | sell 25-delta put credit spread (or sell that strike against existing longs) |
| MOVE | 25-delta credit spread on the product for the root, against the move |
| OVERNIGHT | 25-delta XSP credit spread at the open, against the gap |
| VIX TERM backwardation | sell 25-delta XSP put credit spread, 1 week |
| iv rank rich / implied >> realized / skew spike | sell 25-delta put credit spread on the name |
| iv rank cheap / implied < realized | 50/25-delta call and put debit spreads, his read picks |

Fills, cancels, kernel pushes and the futures snapshots stay as plain lines.

### ES levels

`FUTURES_ES_LEVELS` (default `7700,7720,7740,7800,7840,7880`): a push when the ES front
month crosses any level in either direction (`ES < 7740`, `ES > 7800`), once per level
per `FUTURES_LEVEL_COOLDOWN_MINUTES` (30). The first observation after a deploy only
records which side price is on.
